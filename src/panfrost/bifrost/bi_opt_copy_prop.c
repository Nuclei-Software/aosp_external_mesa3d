/*
 * Copyright (C) 2018 Alyssa Rosenzweig
 * Copyright (C) 2019-2020 Collabora, Ltd.
 *
 * Permission is hereby granted, free of charge, to any person obtaining a
 * copy of this software and associated documentation files (the "Software"),
 * to deal in the Software without restriction, including without limitation
 * the rights to use, copy, modify, merge, publish, distribute, sublicense,
 * and/or sell copies of the Software, and to permit persons to whom the
 * Software is furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice (including the next
 * paragraph) shall be included in all copies or substantial portions of the
 * Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL
 * THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */

#include "compiler.h"
#include "bi_builder.h"

/* A simple scalar-only SSA-based copy-propagation pass. TODO: vectors */

static bool
bi_is_copy(bi_instr *ins)
{
        return (ins->op == BI_OPCODE_MOV_I32) && bi_is_ssa(ins->dest[0])
                && (bi_is_ssa(ins->src[0]) || ins->src[0].type == BI_INDEX_FAU
                                || ins->src[0].type == BI_INDEX_CONSTANT);
}

static bool
bi_reads_fau(bi_instr *ins)
{
        bi_foreach_src(ins, s) {
                if (ins->src[s].type == BI_INDEX_FAU)
                        return true;
        }

        return false;
}

void
bi_opt_copy_prop(bi_context *ctx)
{
        /* Chase SPLIT of COLLECT. Instruction selection usually avoids this
         * pattern (due to the split cache), but it is inevitably generated by
         * the UBO pushing pass.
         */
        bi_instr **collects = calloc(sizeof(bi_instr *), ctx->ssa_alloc);
        bi_foreach_instr_global_safe(ctx, I) {
                if (I->op == BI_OPCODE_COLLECT_I32) {
                        /* Rewrite trivial collects while we're at it */
                        if (I->nr_srcs == 1)
                                I->op = BI_OPCODE_MOV_I32;

                        if (bi_is_ssa(I->dest[0]))
                                collects[I->dest[0].value] = I;
                } else if (I->op == BI_OPCODE_SPLIT_I32) {
                        /* Rewrite trivial splits while we're at it */
                        if (I->nr_dests == 1)
                                I->op = BI_OPCODE_MOV_I32;

                        if (!bi_is_ssa(I->src[0]))
                                continue;

                        bi_instr *collect = collects[I->src[0].value];
                        if (!collect)
                                continue;

                        /* Lower the split to moves, copyprop cleans up */
                        bi_builder b = bi_init_builder(ctx, bi_before_instr(I));

                        for (unsigned d = 0; d < I->nr_dests; ++d)
                                bi_mov_i32_to(&b, I->dest[d], collect->src[d]);

                        bi_remove_instruction(I);
                }
        }

        free(collects);

        bi_index *replacement = calloc(sizeof(bi_index), ctx->ssa_alloc);

        bi_foreach_instr_global_safe(ctx, ins) {
                if (bi_is_copy(ins)) {
                        bi_index replace = ins->src[0];

                        /* Peek through one layer so copyprop converges in one
                         * iteration for chained moves */
                        if (bi_is_ssa(replace)) {
                                bi_index chained = replacement[replace.value];

                                if (!bi_is_null(chained))
                                        replace = chained;
                        }

                        replacement[ins->dest[0].value] = replace;
                }

                bi_foreach_src(ins, s) {
                        bi_index use = ins->src[s];

                        if (use.type != BI_INDEX_NORMAL || use.reg) continue;
                        if (bi_is_staging_src(ins, s)) continue;

                        bi_index repl = replacement[use.value];

                        if (repl.type == BI_INDEX_CONSTANT && bi_reads_fau(ins))
                                continue;

                        if (!bi_is_null(repl))
                                ins->src[s] = bi_replace_index(ins->src[s], repl);
                }
        }

        free(replacement);
}