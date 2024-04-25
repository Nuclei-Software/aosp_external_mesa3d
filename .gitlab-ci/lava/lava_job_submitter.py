#!/usr/bin/env python3
#
# Copyright (C) 2020 - 2022 Collabora Limited
# Authors:
#     Gustavo Padovan <gustavo.padovan@collabora.com>
#     Guilherme Gallo <guilherme.gallo@collabora.com>
#
# SPDX-License-Identifier: MIT

"""Send a job to LAVA, track it and collect log back"""


import argparse
import contextlib
import pathlib
import re
import sys
import time
import traceback
import urllib.parse
import xmlrpc.client
from datetime import datetime, timedelta
from os import getenv
from typing import Any, Optional

import lavacli
import yaml
from lava.exceptions import (
    MesaCIException,
    MesaCIKnownIssueException,
    MesaCIParseException,
    MesaCIRetryError,
    MesaCITimeoutError,
)
from lava.utils import (
    CONSOLE_LOG,
    GitlabSection,
    LogFollower,
    LogSectionType,
    fatal_err,
    hide_sensitive_data,
    print_log,
)
from lavacli.utils import loader

# Timeout in seconds to decide if the device from the dispatched LAVA job has
# hung or not due to the lack of new log output.
DEVICE_HANGING_TIMEOUT_SEC = int(getenv("LAVA_DEVICE_HANGING_TIMEOUT_SEC",  5*60))

# How many seconds the script should wait before try a new polling iteration to
# check if the dispatched LAVA job is running or waiting in the job queue.
WAIT_FOR_DEVICE_POLLING_TIME_SEC = int(getenv("LAVA_WAIT_FOR_DEVICE_POLLING_TIME_SEC", 10))

# How many seconds to wait between log output LAVA RPC calls.
LOG_POLLING_TIME_SEC = int(getenv("LAVA_LOG_POLLING_TIME_SEC", 5))

# How many retries should be made when a timeout happen.
NUMBER_OF_RETRIES_TIMEOUT_DETECTION = int(getenv("LAVA_NUMBER_OF_RETRIES_TIMEOUT_DETECTION", 2))

# How many attempts should be made when a timeout happen during LAVA device boot.
NUMBER_OF_ATTEMPTS_LAVA_BOOT = int(getenv("LAVA_NUMBER_OF_ATTEMPTS_LAVA_BOOT", 3))


def generate_lava_yaml(args):
    # General metadata and permissions, plus also inexplicably kernel arguments
    values = {
        'job_name': 'mesa: {}'.format(args.pipeline_info),
        'device_type': args.device_type,
        'visibility': { 'group': [ args.visibility_group ] },
        'priority': 75,
        'context': {
            'extra_nfsroot_args': ' init=/init rootwait usbcore.quirks=0bda:8153:k'
        },
        "timeouts": {
            "job": {"minutes": args.job_timeout},
            "action": {"minutes": 3},
            "actions": {
                "depthcharge-action": {
                    "minutes": 3 * NUMBER_OF_ATTEMPTS_LAVA_BOOT,
                }
            }
        },
    }

    if args.lava_tags:
        values['tags'] = args.lava_tags.split(',')

    # URLs to our kernel rootfs to boot from, both generated by the base
    # container build
    deploy = {
      'timeout': { 'minutes': 10 },
      'to': 'tftp',
      'os': 'oe',
      'kernel': {
        'url': '{}/{}'.format(args.kernel_url_prefix, args.kernel_image_name),
      },
      'nfsrootfs': {
        'url': '{}/lava-rootfs.tgz'.format(args.rootfs_url_prefix),
        'compression': 'gz',
      }
    }
    if args.kernel_image_type:
        deploy['kernel']['type'] = args.kernel_image_type
    if args.dtb:
        deploy['dtb'] = {
          'url': '{}/{}.dtb'.format(args.kernel_url_prefix, args.dtb)
        }

    # always boot over NFS
    boot = {
        "failure_retry": NUMBER_OF_ATTEMPTS_LAVA_BOOT,
        "method": args.boot_method,
        "commands": "nfs",
        "prompts": ["lava-shell:"],
    }

    # skeleton test definition: only declaring each job as a single 'test'
    # since LAVA's test parsing is not useful to us
    run_steps = []
    test = {
      'timeout': { 'minutes': args.job_timeout },
      'failure_retry': 1,
      'definitions': [ {
        'name': 'mesa',
        'from': 'inline',
        'lava-signal': 'kmsg',
        'path': 'inline/mesa.yaml',
        'repository': {
          'metadata': {
            'name': 'mesa',
            'description': 'Mesa test plan',
            'os': [ 'oe' ],
            'scope': [ 'functional' ],
            'format': 'Lava-Test Test Definition 1.0',
          },
          'run': {
            "steps": run_steps
          },
        },
      } ],
    }

    # job execution script:
    #   - inline .gitlab-ci/common/init-stage1.sh
    #   - fetch and unpack per-pipeline build artifacts from build job
    #   - fetch and unpack per-job environment from lava-submit.sh
    #   - exec .gitlab-ci/common/init-stage2.sh

    with open(args.first_stage_init, 'r') as init_sh:
      run_steps += [ x.rstrip() for x in init_sh if not x.startswith('#') and x.rstrip() ]

    if args.jwt_file:
        with open(args.jwt_file) as jwt_file:
            run_steps += [
                "set +x",
                f'echo -n "{jwt_file.read()}" > "{args.jwt_file}"  # HIDEME',
                "set -x",
                f'echo "export CI_JOB_JWT_FILE={args.jwt_file}" >> /set-job-env-vars.sh',
            ]
    else:
        run_steps += [
            "echo Could not find jwt file, disabling MINIO requests...",
            "sed -i '/MINIO_RESULTS_UPLOAD/d' /set-job-env-vars.sh",
        ]

    run_steps += [
      'mkdir -p {}'.format(args.ci_project_dir),
      'wget -S --progress=dot:giga -O- {} | tar -xz -C {}'.format(args.build_url, args.ci_project_dir),
      'wget -S --progress=dot:giga -O- {} | tar -xz -C /'.format(args.job_rootfs_overlay_url),

      # Sleep a bit to give time for bash to dump shell xtrace messages into
      # console which may cause interleaving with LAVA_SIGNAL_STARTTC in some
      # devices like a618.
      'sleep 1',

      # Putting CI_JOB name as the testcase name, it may help LAVA farm
      # maintainers with monitoring
      f"lava-test-case 'mesa-ci_{args.mesa_job_name}' --shell /init-stage2.sh",
    ]

    values['actions'] = [
      { 'deploy': deploy },
      { 'boot': boot },
      { 'test': test },
    ]

    return yaml.dump(values, width=10000000)


def setup_lava_proxy():
    config = lavacli.load_config("default")
    uri, usr, tok = (config.get(key) for key in ("uri", "username", "token"))
    uri_obj = urllib.parse.urlparse(uri)
    uri_str = "{}://{}:{}@{}{}".format(uri_obj.scheme, usr, tok, uri_obj.netloc, uri_obj.path)
    transport = lavacli.RequestsTransport(
        uri_obj.scheme,
        config.get("proxy"),
        config.get("timeout", 120.0),
        config.get("verify_ssl_cert", True),
    )
    proxy = xmlrpc.client.ServerProxy(
        uri_str, allow_none=True, transport=transport)

    print_log("Proxy for {} created.".format(config['uri']))

    return proxy


def _call_proxy(fn, *args):
    retries = 60
    for n in range(1, retries + 1):
        try:
            return fn(*args)
        except xmlrpc.client.ProtocolError as err:
            if n == retries:
                traceback.print_exc()
                fatal_err("A protocol error occurred (Err {} {})".format(err.errcode, err.errmsg))
            else:
                time.sleep(15)
        except xmlrpc.client.Fault as err:
            traceback.print_exc()
            fatal_err("FATAL: Fault: {} (code: {})".format(err.faultString, err.faultCode))


class LAVAJob:
    COLOR_STATUS_MAP = {
        "pass": CONSOLE_LOG["FG_GREEN"],
        "hung": CONSOLE_LOG["FG_YELLOW"],
        "fail": CONSOLE_LOG["FG_RED"],
        "canceled": CONSOLE_LOG["FG_MAGENTA"],
    }

    def __init__(self, proxy, definition):
        self.job_id = None
        self.proxy = proxy
        self.definition = definition
        self.last_log_line = 0
        self.last_log_time = None
        self.is_finished = False
        self.status = "created"

    def heartbeat(self):
        self.last_log_time = datetime.now()
        self.status = "running"

    def validate(self) -> Optional[dict]:
        """Returns a dict with errors, if the validation fails.

        Returns:
            Optional[dict]: a dict with the validation errors, if any
        """
        return _call_proxy(self.proxy.scheduler.jobs.validate, self.definition, True)

    def submit(self):
        try:
            self.job_id = _call_proxy(self.proxy.scheduler.jobs.submit, self.definition)
        except MesaCIException:
            return False
        return True

    def cancel(self):
        if self.job_id:
            self.proxy.scheduler.jobs.cancel(self.job_id)

    def is_started(self) -> bool:
        waiting_states = ["Submitted", "Scheduling", "Scheduled"]
        job_state: dict[str, str] = _call_proxy(
            self.proxy.scheduler.job_state, self.job_id
        )
        return job_state["job_state"] not in waiting_states

    def _load_log_from_data(self, data) -> list[str]:
        lines = []
        # When there is no new log data, the YAML is empty
        if loaded_lines := yaml.load(str(data), Loader=loader(False)):
            lines = loaded_lines
            self.last_log_line += len(lines)
        return lines

    def get_logs(self) -> list[str]:
        try:
            (finished, data) = _call_proxy(
                self.proxy.scheduler.jobs.logs, self.job_id, self.last_log_line
            )
            self.is_finished = finished
            return self._load_log_from_data(data)

        except Exception as mesa_ci_err:
            raise MesaCIParseException(
                f"Could not get LAVA job logs. Reason: {mesa_ci_err}"
            ) from mesa_ci_err

    def parse_job_result_from_log(
        self, lava_lines: list[dict[str, str]]
    ) -> list[dict[str, str]]:
        """Use the console log to catch if the job has completed successfully or
        not. Returns the list of log lines until the result line."""

        last_line = None  # Print all lines. lines[:None] == lines[:]

        for idx, line in enumerate(lava_lines):
            if result := re.search(r"hwci: mesa: (pass|fail)", line):
                self.is_finished = True
                self.status = result.group(1)

                last_line = idx + 1
                # We reached the log end here. hwci script has finished.
                break
        return lava_lines[:last_line]


def find_exception_from_metadata(metadata, job_id):
    if "result" not in metadata or metadata["result"] != "fail":
        return
    if "error_type" in metadata:
        error_type = metadata["error_type"]
        if error_type == "Infrastructure":
            raise MesaCIException(
                f"LAVA job {job_id} failed with Infrastructure Error. Retry."
            )
        if error_type == "Job":
            # This happens when LAVA assumes that the job cannot terminate or
            # with mal-formed job definitions. As we are always validating the
            # jobs, only the former is probable to happen. E.g.: When some LAVA
            # action timed out more times than expected in job definition.
            raise MesaCIException(
                f"LAVA job {job_id} failed with JobError "
                "(possible LAVA timeout misconfiguration/bug). Retry."
            )
    if "case" in metadata and metadata["case"] == "validate":
        raise MesaCIException(
            f"LAVA job {job_id} failed validation (possible download error). Retry."
        )
    return metadata


def find_lava_error(job) -> None:
    # Look for infrastructure errors and retry if we see them.
    results_yaml = _call_proxy(job.proxy.results.get_testjob_results_yaml, job.job_id)
    results = yaml.load(results_yaml, Loader=loader(False))
    for res in results:
        metadata = res["metadata"]
        find_exception_from_metadata(metadata, job.job_id)

    # If we reach this far, it means that the job ended without hwci script
    # result and no LAVA infrastructure problem was found
    job.status = "fail"


def show_job_data(job):
    with GitlabSection(
        "job_data",
        "LAVA job info",
        type=LogSectionType.LAVA_POST_PROCESSING,
        start_collapsed=True,
    ):
        show = _call_proxy(job.proxy.scheduler.jobs.show, job.job_id)
        for field, value in show.items():
            print("{}\t: {}".format(field, value))


def fetch_logs(job, max_idle_time, log_follower) -> None:
    # Poll to check for new logs, assuming that a prolonged period of
    # silence means that the device has died and we should try it again
    if datetime.now() - job.last_log_time > max_idle_time:
        max_idle_time_min = max_idle_time.total_seconds() / 60

        raise MesaCITimeoutError(
            f"{CONSOLE_LOG['BOLD']}"
            f"{CONSOLE_LOG['FG_YELLOW']}"
            f"LAVA job {job.job_id} does not respond for {max_idle_time_min} "
            "minutes. Retry."
            f"{CONSOLE_LOG['RESET']}",
            timeout_duration=max_idle_time,
        )

    time.sleep(LOG_POLLING_TIME_SEC)

    # The XMLRPC binary packet may be corrupted, causing a YAML scanner error.
    # Retry the log fetching several times before exposing the error.
    for _ in range(5):
        with contextlib.suppress(MesaCIParseException):
            new_log_lines = job.get_logs()
            break
    else:
        raise MesaCIParseException

    if log_follower.feed(new_log_lines):
        # If we had non-empty log data, we can assure that the device is alive.
        job.heartbeat()
    parsed_lines = log_follower.flush()

    # Only parse job results when the script reaches the end of the logs.
    # Depending on how much payload the RPC scheduler.jobs.logs get, it may
    # reach the LAVA_POST_PROCESSING phase.
    if log_follower.current_section.type in (
        LogSectionType.TEST_CASE,
        LogSectionType.LAVA_POST_PROCESSING,
    ):
        parsed_lines = job.parse_job_result_from_log(parsed_lines)

    for line in parsed_lines:
        print_log(line)


def follow_job_execution(job):
    try:
        job.submit()
    except Exception as mesa_ci_err:
        raise MesaCIException(
            f"Could not submit LAVA job. Reason: {mesa_ci_err}"
        ) from mesa_ci_err

    print_log(f"Waiting for job {job.job_id} to start.")
    while not job.is_started():
        time.sleep(WAIT_FOR_DEVICE_POLLING_TIME_SEC)
    print_log(f"Job {job.job_id} started.")

    gl = GitlabSection(
        id="lava_boot",
        header="LAVA boot",
        type=LogSectionType.LAVA_BOOT,
        start_collapsed=True,
    )
    print(gl.start())
    max_idle_time = timedelta(seconds=DEVICE_HANGING_TIMEOUT_SEC)
    with LogFollower(current_section=gl) as lf:

        max_idle_time = timedelta(seconds=DEVICE_HANGING_TIMEOUT_SEC)
        # Start to check job's health
        job.heartbeat()
        while not job.is_finished:
            fetch_logs(job, max_idle_time, lf)

    show_job_data(job)

    # Mesa Developers expect to have a simple pass/fail job result.
    # If this does not happen, it probably means a LAVA infrastructure error
    # happened.
    if job.status not in ["pass", "fail"]:
        find_lava_error(job)


def print_job_final_status(job):
    if job.status == "running":
        job.status = "hung"

    color = LAVAJob.COLOR_STATUS_MAP.get(job.status, CONSOLE_LOG["FG_RED"])
    print_log(
        f"{color}"
        f"LAVA Job finished with status: {job.status}"
        f"{CONSOLE_LOG['RESET']}"
    )


def retriable_follow_job(proxy, job_definition) -> LAVAJob:
    retry_count = NUMBER_OF_RETRIES_TIMEOUT_DETECTION

    for attempt_no in range(1, retry_count + 2):
        job = LAVAJob(proxy, job_definition)
        try:
            follow_job_execution(job)
            return job
        except MesaCIKnownIssueException as found_issue:
            print_log(found_issue)
            job.status = "canceled"
        except MesaCIException as mesa_exception:
            print_log(mesa_exception)
            job.cancel()
        except KeyboardInterrupt as e:
            print_log("LAVA job submitter was interrupted. Cancelling the job.")
            job.cancel()
            raise e
        finally:
            print_log(
                f"{CONSOLE_LOG['BOLD']}"
                f"Finished executing LAVA job in the attempt #{attempt_no}"
                f"{CONSOLE_LOG['RESET']}"
            )
            print_job_final_status(job)

    raise MesaCIRetryError(
        f"{CONSOLE_LOG['BOLD']}"
        f"{CONSOLE_LOG['FG_RED']}"
        "Job failed after it exceeded the number of "
        f"{retry_count} retries."
        f"{CONSOLE_LOG['RESET']}",
        retry_count=retry_count,
    )


def treat_mesa_job_name(args):
    # Remove mesa job names with spaces, which breaks the lava-test-case command
    args.mesa_job_name = args.mesa_job_name.split(" ")[0]


def main(args):
    proxy = setup_lava_proxy()

    job_definition = generate_lava_yaml(args)

    if args.dump_yaml:
        with GitlabSection(
            "yaml_dump",
            "LAVA job definition (YAML)",
            type=LogSectionType.LAVA_BOOT,
            start_collapsed=True,
        ):
            print(hide_sensitive_data(job_definition))
    job = LAVAJob(proxy, job_definition)

    if errors := job.validate():
        fatal_err(f"Error in LAVA job definition: {errors}")
    print_log("LAVA job definition validated successfully")

    if args.validate_only:
        return

    finished_job = retriable_follow_job(proxy, job_definition)
    exit_code = 0 if finished_job.status == "pass" else 1
    sys.exit(exit_code)


def create_parser():
    parser = argparse.ArgumentParser("LAVA job submitter")

    parser.add_argument("--pipeline-info")
    parser.add_argument("--rootfs-url-prefix")
    parser.add_argument("--kernel-url-prefix")
    parser.add_argument("--build-url")
    parser.add_argument("--job-rootfs-overlay-url")
    parser.add_argument("--job-timeout", type=int)
    parser.add_argument("--first-stage-init")
    parser.add_argument("--ci-project-dir")
    parser.add_argument("--device-type")
    parser.add_argument("--dtb", nargs='?', default="")
    parser.add_argument("--kernel-image-name")
    parser.add_argument("--kernel-image-type", nargs='?', default="")
    parser.add_argument("--boot-method")
    parser.add_argument("--lava-tags", nargs='?', default="")
    parser.add_argument("--jwt-file", type=pathlib.Path)
    parser.add_argument("--validate-only", action='store_true')
    parser.add_argument("--dump-yaml", action='store_true')
    parser.add_argument("--visibility-group")
    parser.add_argument("--mesa-job-name")

    return parser


if __name__ == "__main__":
    # given that we proxy from DUT -> LAVA dispatcher -> LAVA primary -> us ->
    # GitLab runner -> GitLab primary -> user, safe to say we don't need any
    # more buffering
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

    parser = create_parser()

    parser.set_defaults(func=main)
    args = parser.parse_args()
    treat_mesa_job_name(args)
    args.func(args)
