#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#include "eval.h"
#include "phase1.h"
#include "phase2.h"

int main(int argc, char **argv) {
    long long seed = 0; int have = 0, ymin = -64, ymax = 128, ncx = 0; int cxs[64], czs[64];
    int cx = 0, cz = 0, hc = 0;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--seed") && i + 1 < argc) { seed = atoll(argv[++i]); have = 1; }
        else if (!strcmp(argv[i], "--cx") && i + 1 < argc) { cx = atoi(argv[++i]); hc |= 1; }
        else if (!strcmp(argv[i], "--cz") && i + 1 < argc) { cz = atoi(argv[++i]); hc |= 2; }
        else if (!strcmp(argv[i], "--chunk") && i + 2 < argc && ncx < 64) { cxs[ncx] = atoi(argv[++i]); czs[ncx] = atoi(argv[++i]); ncx++; }
        else if (!strcmp(argv[i], "--ymin") && i + 1 < argc) ymin = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--ymax") && i + 1 < argc) ymax = atoi(argv[++i]);
        else { fprintf(stderr, "terrain_dump: unknown arg %s\n", argv[i]); return 2; }
    }
    if (hc == 3 && ncx < 64) { cxs[ncx] = cx; czs[ncx] = cz; ncx++; }
    if (!have || !ncx) { fprintf(stderr, "usage: terrain_dump --seed S --cx CX --cz CZ [--ymin -64] [--ymax 128]\n"); return 2; }
#ifdef _WIN32
    { extern int _setmode(int, int); _setmode(1, 0x8000); }
#endif
    lysh_phase1_opts opts; memset(&opts, 0, sizeof opts); opts.large_biomes = 0; opts.pre_26_2 = 1;
    lysh_search_ctx ctx; lysh_search_ctx_init(&ctx, (uint64_t)seed, &opts, 0);
    lysh_phase2 *p2 = &ctx.p2;
    if (getenv("TD_PROBE") && atoi(getenv("TD_PROBE")) == 2) {
        static const int gx[6] = { 0, 6, 3, 3, 0, 6 }, gz[6] = { 0, 0, 4, 0, 4, 4 };
        for (int c = 0; c < ncx; c++) {
            double mn[6] = { 1e9, 1e9, 1e9, 1e9, 1e9, 1e9 }, mx[6] = { -1e9, -1e9, -1e9, -1e9, -1e9, -1e9 }; int hmin = 1000, hmax = -1000;
            for (int k = 0; k < 6; k++) {
                const int x = cxs[c] * 16 + gx[k], z = czs[c] * 16 + gz[k];
                lysh_terrain_info ti; lysh_terrain_info_at(&p2->terrain, x >> 2, z >> 2, &ti);
                const double v[6] = { ti.continentalness, ti.erosion, ti.weirdness, ti.point.offset, ti.point.factor, ti.point.peaks };
                for (int q = 0; q < 6; q++) { if (v[q] < mn[q]) mn[q] = v[q]; if (v[q] > mx[q]) mx[q] = v[q]; }
                const int h = lysh_phase2_height_pre_carver(p2, x, z); if (h < hmin) hmin = h; if (h > hmax) hmax = h;
            }
            printf("%d %d  h %d %d  cont %.4f %.4f  ero %.4f %.4f  weird %.4f %.4f  offset %.4f %.4f  factor %.4f %.4f  peaks %.4f %.4f\n", cxs[c], czs[c], hmin, hmax, mn[0], mx[0], mn[1], mx[1], mn[2], mx[2], mn[3], mx[3], mn[4], mx[4], mn[5], mx[5]);
        }
        lysh_search_ctx_free(&ctx); return 0;
    }
    if (getenv("TD_PROBE")) {
        const int ys[] = { 120, 80, 63, 40, 0, -30, -50, -54, -56 };
        for (int c = 0; c < ncx; c++) {
            printf("chunk %d %d\n", cxs[c], czs[c]);
            for (int dz = 0; dz < 9; dz += 4) for (int dx = 0; dx < 7; dx += 3) {
                const int x = cxs[c] * 16 + dx, z = czs[c] * 16 + dz;
                printf("  col(%d,%d) height %d  pre_carver %d  density:", dx, dz, lysh_phase2_height(p2, x, z), lysh_phase2_height_pre_carver(p2, x, z));
                for (unsigned k = 0; k < sizeof ys / sizeof *ys; k++) printf(" y%d=%.3f", ys[k], lysh_phase2_density_at(p2, x, ys[k], z));
                printf("\n");
            }
        }
        lysh_search_ctx_free(&ctx); return 0;
    }
    const int ny = ymax - ymin + 1;
    uint8_t *col = (uint8_t *)malloc((size_t)ny);
    for (int c = 0; c < ncx; c++) {
        int32_t hdr[4] = { cxs[c], czs[c], ymin, ymax };
        fwrite(hdr, sizeof hdr, 1, stdout);
        for (int dx = 0; dx < 16; dx++) for (int dz = 0; dz < 16; dz++) {
            const int x = cxs[c] * 16 + dx, z = czs[c] * 16 + dz;
            for (int y = ymin; y <= ymax; y++) col[y - ymin] = (uint8_t)lysh_phase2_state_after_carvers(p2, x, y, z);
            fwrite(col, 1, (size_t)ny, stdout);
        }
    }
    free(col);
    lysh_search_ctx_free(&ctx);
    return 0;
}
