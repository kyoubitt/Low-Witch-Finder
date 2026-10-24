#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "eval.h"
#include "phase1.h"
#include "phase2.h"

#define CORRIDOR_TOP (-59)

int main(int argc, char **argv) {
    long long X = -1; const char* listPath = NULL;
    int wx = -32, wz = 16, fw = 9, fd = 7, ulo = 0, uhi = 65536, reject = -40, topn = 10;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--x") && i + 1 < argc) X = atoll(argv[++i]);
        else if (!strcmp(argv[i], "--list") && i + 1 < argc) listPath = argv[++i];
        else if (!strcmp(argv[i], "--wx") && i + 1 < argc) wx = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--wz") && i + 1 < argc) wz = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--shape") && i + 1 < argc) { i++; if (!strcmp(argv[i], "7x9")) { fw = 7; fd = 9; } }
        else if (!strcmp(argv[i], "--u-lo") && i + 1 < argc) ulo = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--u-hi") && i + 1 < argc) uhi = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--reject") && i + 1 < argc) reject = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--top") && i + 1 < argc) topn = atoi(argv[++i]);
        else { fprintf(stderr, "skyscan: unknown arg %s\n", argv[i]); return 2; }
    }
    if (listPath) {
        FILE* lf = fopen(listPath, "r");
        if (!lf) { fprintf(stderr, "skyscan: cannot read %s\n", listPath); return 1; }
        char ln[512];
        if (!fgets(ln, sizeof ln, lf)) return 1;
        while (fgets(ln, sizeof ln, lf)) {
            unsigned long long LX; int lu, lwx, lwz, lfw, lfd, ldx, ldz, lb;
            if (sscanf(ln, "%llu,%d,%d,%d,%d,%d,%d,%d,%d", &LX, &lu, &lwx, &lwz, &lfw, &lfd, &ldx, &ldz, &lb) != 9) continue;
            const long long w = (long long)(((unsigned long long)(unsigned)lu << 48) | LX);
            lysh_phase1_opts opts;
            memset(&opts, 0, sizeof opts);
            opts.pre_26_2 = 1;
            lysh_search_ctx ctx;
            lysh_search_ctx_init(&ctx, (uint64_t)w, &opts, 0);
            lysh_phase2 *p2 = &ctx.p2;
            int sum = 0, mn = 1000, mx = -1000, le54 = 0, lava = 0, solid = 0, nw = 0, ne = 0, sw = 0, se = 0, hi52 = 0;
            for (int dz = 0; dz < lfd; dz++) for (int dx = 0; dx < lfw; dx++) {
                const int x = lwx + dx, z = lwz + dz, h = lysh_phase2_height(p2, x, z);
                sum += h; if (h < mn) mn = h; if (h > mx) mx = h; if (h <= -54) le54++; if (h > -53) hi52++;
                if (dx == 0 && dz == 0) nw = h; if (dx == lfw - 1 && dz == 0) ne = h; if (dx == 0 && dz == lfd - 1) sw = h; if (dx == lfw - 1 && dz == lfd - 1) se = h;
                for (int y = CORRIDOR_TOP; y < h; y++) {
                    const lysh_block_state s = lysh_phase2_state_after_carvers(p2, x, y, z);
                    if (s == LYSH_BLOCK_LAVA) lava++; else if (s == LYSH_BLOCK_NULL) solid++;
                }
            }
            printf("R mean %.2f min %d max %d le54 %d lava %d solid %d seed %lld X %llu u %d win %d,%d %dx%d off %d,%d bound %d corners NW %d NE %d SW %d SE %d above-53 %d\n",
                   (double)sum / (lfw * lfd), mn, mx, le54, lava, solid, w, LX, lu, lwx, lwz, lfw, lfd, ldx, ldz, lb, nw, ne, sw, se, hi52);
            fflush(stdout);
            lysh_search_ctx_free(&ctx);
        }
        fclose(lf);
        return 0;
    }
    if (X < 0) { fprintf(stderr, "usage: skyscan --x X [...]\n"); return 2; }

    int hist[40]; memset(hist, 0, sizeof hist);
    int scanned = 0, survived = 0;
    double bestMean[64]; int bestU[64]; int nb = 0;
    if (topn > 64) topn = 64;

    for (int u = ulo; u < uhi; u++) {
        const long long w = (long long)(((unsigned long long)(unsigned)u << 48) | (unsigned long long)X);
        lysh_phase1_opts opts;
        memset(&opts, 0, sizeof opts);
        opts.pre_26_2 = 1;
        lysh_search_ctx ctx;
        lysh_search_ctx_init(&ctx, (uint64_t)w, &opts, 0);
        lysh_phase2 *p2 = &ctx.p2;
        scanned++;

        int sum = 0, mn = 1000, mx = -1000, le54 = 0, bad = 0;
        for (int dz = 0; dz < fd && !bad; dz++) for (int dx = 0; dx < fw; dx++) {
            const int h = lysh_phase2_height(p2, wx + dx, wz + dz);
            if (h > reject) { bad = 1; break; }
            sum += h; if (h < mn) mn = h; if (h > mx) mx = h; if (h <= -54) le54++;
        }
        if (!bad) {
            survived++;
            const int n = fw * fd;
            const double mean = (double)sum / n;
            int lava = 0, solid = 0;
            for (int dz = 0; dz < fd; dz++) for (int dx = 0; dx < fw; dx++) {
                const int x = wx + dx, z = wz + dz, h = lysh_phase2_height(p2, x, z);
                for (int y = CORRIDOR_TOP; y < h; y++) {
                    const lysh_block_state s = lysh_phase2_state_after_carvers(p2, x, y, z);
                    if (s == LYSH_BLOCK_LAVA) lava++; else if (s == LYSH_BLOCK_NULL) solid++;
                }
            }
            printf("U %d mean %.2f min %d max %d le54 %d lava %d solid %d\n", u, mean, mn, mx, le54, lava, solid);
            int b = (int)((mean + 70.0) / 10.0); if (b < 0) b = 0; if (b > 39) b = 39; hist[b]++;

            int pos = nb;
            while (pos > 0 && bestMean[pos - 1] > mean) pos--;
            if (pos < topn) {
                int last = nb < topn ? nb : topn - 1;
                for (int k = last; k > pos; k--) { bestMean[k] = bestMean[k - 1]; bestU[k] = bestU[k - 1]; }
                bestMean[pos] = mean; bestU[pos] = u; if (nb < topn) nb++;
            }
        }
        lysh_search_ctx_free(&ctx);
    }
    printf("SUMMARY x %lld u %d..%d window x %d z %d %dx%d scanned %d survived-reject(%d) %d\n", X, ulo, uhi, wx, wz, fw, fd, scanned, reject, survived);
    for (int b = 0; b < 40; b++) if (hist[b]) printf("HIST mean H %d..%d : %d\n", b * 10 - 70, b * 10 - 61, hist[b]);
    for (int k = 0; k < nb; k++) printf("BEST u %d mean %.2f\n", bestU[k], bestMean[k]);
    return 0;
}
