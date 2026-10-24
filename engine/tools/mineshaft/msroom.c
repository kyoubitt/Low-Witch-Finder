#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "eval.h"
#include "phase1.h"
#include "phase2.h"
#include "mineshaft_carve.h"

#define MAX_BOXES 512

static int footprint_sum(lysh_phase2 *p2, int hut_x, int hut_z, int axis_z,
                         const int *boxes, int n_boxes) {
    int w = axis_z ? 7 : 9;
    int d = axis_z ? 9 : 7;
    int sum = 0;
    for (int dx = 0; dx < w; dx++) {
        for (int dz = 0; dz < d; dz++) {
            int x = hut_x + dx, z = hut_z + dz;
            sum += (n_boxes > 0)
                 ? lysh_phase2_height_excluding(p2, x, z, boxes, n_boxes)
                 : lysh_phase2_height(p2, x, z);
        }
    }
    return sum;
}

static void guard_chunk(lysh_phase2 *p2, const int *b, int ccx, int ccz,
                        int *lava, int *water) {
    const int cminx = ccx * 16, cmaxx = ccx * 16 + 15, cminz = ccz * 16, cmaxz = ccz * 16 + 15;
    const int i = b[0] - 1 > cminx ? b[0] - 1 : cminx;
    const int j = b[1] - 1 > -64 ? b[1] - 1 : -64;
    const int k = b[2] - 1 > cminz ? b[2] - 1 : cminz;
    const int l = b[3] + 1 < cmaxx ? b[3] + 1 : cmaxx;
    const int m = b[4] + 1 < 319 ? b[4] + 1 : 319;
    const int n = b[5] + 1 < cmaxz ? b[5] + 1 : cmaxz;
#define G(X, Y, Z) do { lysh_block_state s_ = lysh_phase2_state_after_carvers(p2, (X), (Y), (Z)); \
                        if (s_ == LYSH_BLOCK_LAVA) (*lava)++; else if (s_ == LYSH_BLOCK_WATER) (*water)++; } while (0)
    for (int o = i; o <= l; o++) for (int p = k; p <= n; p++) { G(o, j, p); G(o, m, p); }
    for (int o = i; o <= l; o++) for (int p = j; p <= m; p++) { G(o, p, k); G(o, p, n); }
    for (int o = k; o <= n; o++) for (int p = j; p <= m; p++) { G(i, p, o); G(l, p, o); }
#undef G
}

static int natcb(void* c, int x, int y, int z) {
    const lysh_block_state s = lysh_phase2_state_after_carvers((lysh_phase2*)c, x, y, z);
    return s == LYSH_BLOCK_NULL ? 0 : s == LYSH_BLOCK_AIR ? 1 : s == LYSH_BLOCK_WATER ? 2 : 3;
}

int main(int argc, char **argv) {
    long long seed = 0;
    int hut_x = 0, hut_z = 0, have = 0, large_biomes = 0, guard = 0, exact_cols = 0, multi = 0;

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--seed") && i + 1 < argc) { seed = atoll(argv[++i]); have |= 1; }
        else if (!strcmp(argv[i], "--x") && i + 1 < argc) { hut_x = atoi(argv[++i]); have |= 2; }
        else if (!strcmp(argv[i], "--z") && i + 1 < argc) { hut_z = atoi(argv[++i]); have |= 4; }
        else if (!strcmp(argv[i], "--large-biomes")) { large_biomes = 1; }
        else if (!strcmp(argv[i], "--guard")) { guard = 1; }
        else if (!strcmp(argv[i], "--multi")) { multi = 1; }
        else if (!strcmp(argv[i], "--exact-cols")) { exact_cols = 1; }
        else { fprintf(stderr, "msroom: unknown arg %s\n", argv[i]); return 2; }
    }
    if (have != 7) {
        fprintf(stderr, "usage: msroom --seed S --x X --z Z  (boxes on stdin)\n");
        return 2;
    }

    static McPiece pcs[MAX_BOXES];
    int n_pcs = 0;
    static int boxes[7 * MAX_BOXES];
    int n_boxes = 0;
    char line[512];
    while (!multi && n_boxes < MAX_BOXES && fgets(line, sizeof line, stdin)) {
        { char kd; McPiece pp; pp.rails = 0; if (sscanf(line, " %c %d %d %d %d %d %d %d %d %d", &kd, &pp.x0, &pp.y0, &pp.z0, &pp.x1, &pp.y1, &pp.z1, &pp.facing, &pp.parent, &pp.rails) >= 9 && kd >= 'A' && kd <= 'Z') { pp.kind = kd; pcs[n_pcs++] = pp; continue; } }
        int *q = boxes + 7 * n_boxes;
        q[6] = 0;
        if (sscanf(line, "%d %d %d %d %d %d %d",
                   q, q + 1, q + 2, q + 3, q + 4, q + 5, q + 6) >= 6) {
            n_boxes++;
        }
    }

    lysh_phase1_opts opts;
    memset(&opts, 0, sizeof(opts));
    opts.large_biomes = large_biomes;

    opts.pre_26_2  = 1;

    { const char* e = getenv("MC_EPS"); if (e) mc_dome_eps = (float)atof(e); }
    { const char* e = getenv("MC_NOPLANKS"); if (e) mc_noplanks = atoi(e); }
    { const char* e = getenv("MC_TRACE"); if (e) sscanf(e, "%d,%d,%d", &mc_trace_x, &mc_trace_y, &mc_trace_z); }
    lysh_search_ctx ctx;
    lysh_search_ctx_init(&ctx, (uint64_t) seed, &opts, 0);
    lysh_phase2 *p2p = &ctx.p2;

    int dir = lysh_hut_orientation((uint64_t) seed, hut_x, hut_z);
    int axis_z = (dir == 0 || dir == 2);
    { const char* sh = getenv("MC_SHAPE"); if (sh) axis_z = (sh[0] == '7'); }

    if (multi) {

        static McWorld w; static unsigned char natcache[16 * 16 * MC_YN]; mc_natc = natcache;
        static McPiece mp[MAX_BOXES]; int np = 0; char ln[512]; char id[64]; id[0] = 0; int have_tree = 0;
        const int fw = axis_z ? 7 : 9, fd = axis_z ? 9 : 7;
        for (;;) {
            const char* got = fgets(ln, sizeof ln, stdin);
            if (!got || !strncmp(ln, "TREE", 4)) {
                if (have_tree && np > 0) {
                    long s0 = 0, s1 = 0, sg = 0; int ref = 0;
                    for (int top = 0; top < 2; top++) {
                        mc_init(&w, hut_x & ~15, hut_z & ~15, natcb, p2p, top); mc_apply(&w, mp, np);
                        long s = 0; for (int dx = 0; dx < fw; dx++) for (int dz = 0; dz < fd; dz++) s += mc_height(&w, hut_x + dx, hut_z + dz);
                        if (top == 0) s0 = s; else { s1 = s; ref = w.refused; }
                    }
                    mc_guard_off = 1; mc_init(&w, hut_x & ~15, hut_z & ~15, natcb, p2p, 1); mc_apply(&w, mp, np);
                    for (int dx = 0; dx < fw; dx++) for (int dz = 0; dz < fd; dz++) sg += mc_height(&w, hut_x + dx, hut_z + dz);
                    mc_guard_off = 0;
                    printf("TREE %s %ld %ld %d %ld %ld %ld %ld\n", id, s0 / 63, s1 / 63, ref, sg / 63, s0, s1, sg);
                }
                if (!got) break;
                sscanf(ln + 4, " %63s", id); np = 0; have_tree = 1;
            } else if (np < MAX_BOXES) {
                McPiece pp; memset(&pp, 0, sizeof pp); pp.rails = 0;
                if (sscanf(ln, " %c %d %d %d %d %d %d %d %d %d", &pp.kind, &pp.x0, &pp.y0, &pp.z0, &pp.x1, &pp.y1, &pp.z1, &pp.facing, &pp.parent, &pp.rails) >= 9) mp[np++] = pp;
            }
        }
        lysh_search_ctx_free(&ctx);
        return 0;
    }

    if (n_pcs > 0) {

        static McWorld w;
        static unsigned char natcache[16 * 16 * MC_YN];
        mc_natc = natcache;
        const int fw = axis_z ? 7 : 9, fd = axis_z ? 9 : 7;
        const int sum_nat = footprint_sum(p2p, hut_x, hut_z, axis_z, NULL, 0);
        printf("EXACT %lld %d %d nat %d", seed, hut_x, hut_z, sum_nat / 63);
        for (int top = 0; top < 2; top++) {
            mc_init(&w, hut_x & ~15, hut_z & ~15, natcb, p2p, top);
            mc_apply(&w, pcs, n_pcs);
            long sum = 0;
            for (int dx = 0; dx < fw; dx++) for (int dz = 0; dz < fd; dz++) sum += mc_height(&w, hut_x + dx, hut_z + dz);
            printf("  top%d %ld/63=%ld refused %d", top, sum, sum / 63, w.refused);
            if (getenv("MC_MASK") && top == 1) fprintf(stderr, "refused piece mask (bit = input line) %x\n", w.refusedMask);
            if (exact_cols && top == 1) {
                printf("\n  natural H (no mineshaft) and highest carved y per column (top1):\n");
                for (int dz = 0; dz < fd; dz++) {
                    printf("   ");
                    for (int dx = 0; dx < fw; dx++) {
                        int hy = -999;
                        for (int y = MC_Y0 + MC_YN - 1; y >= MC_Y0; y--) if (w.carved[(hut_x + dx) - w.cx0][(hut_z + dz) - w.cz0][y - MC_Y0]) { hy = y; break; }
                        printf(" %4d/%-4d", lysh_phase2_height(p2p, hut_x + dx, hut_z + dz), hy);
                    }
                    printf("\n");
                }
            }
            if (exact_cols) {
                printf("\n  columns (top%d), z down, x across:\n", top);
                for (int dz = 0; dz < fd; dz++) { printf("   "); for (int dx = 0; dx < fw; dx++) printf(" %4d", mc_height(&w, hut_x + dx, hut_z + dz)); printf("\n"); }
            }

            mc_guard_off = 1;
            mc_init(&w, hut_x & ~15, hut_z & ~15, natcb, p2p, top);
            mc_apply(&w, pcs, n_pcs);
            { long sg = 0; for (int dx = 0; dx < fw; dx++) for (int dz = 0; dz < fd; dz++) sg += mc_height(&w, hut_x + dx, hut_z + dz);
              printf("  ng%d %ld/63=%ld", top, sg, sg / 63); }
            mc_guard_off = 0;
        }
        {
            const char* mt = getenv("MC_TRIALS"); const int trials = mt ? atoi(mt) : 400;
            if (trials > 0) {
            int le[6] = { 0 }; double msum = 0;
            for (int k = 0; k < trials; k++) {
                mc_init(&w, hut_x & ~15, hut_z & ~15, natcb, p2p, 2);
                w.rng = 0x9E3779B97F4A7C15ULL * (uint64_t)(k + 1) ^ 0xD1B54A32D192ED03ULL;
                mc_apply(&w, pcs, n_pcs);
                long sum = 0;
                for (int dx = 0; dx < fw; dx++) for (int dz = 0; dz < fd; dz++) sum += mc_height(&w, hut_x + dx, hut_z + dz);
                msum += (double)sum / 63.0;
                for (int j = 0; j < 6; j++) if (sum / 63 <= -55 - j) le[j]++;
            }
            printf("  mc%d mean %.2f P<=-55 %.3f P<=-56 %.3f P<=-57 %.3f P<=-58 %.3f P<=-59 %.3f P<=-60 %.3f", trials, msum / trials,
                   (double)le[0] / trials, (double)le[1] / trials, (double)le[2] / trials, (double)le[3] / trials, (double)le[4] / trials, (double)le[5] / trials);
            }
        }
        printf("\n");
        if (getenv("MC_FINAL")) {
            for (int top = 0; top < 2; top++) {
                mc_init(&w, hut_x & ~15, hut_z & ~15, natcb, p2p, top); mc_apply(&w, pcs, n_pcs);
                for (int x = w.cx0; x < w.cx0 + 16; x++) for (int z = w.cz0; z < w.cz0 + 16; z++) {
                    printf("FINC%d %lld %d %d ", top, seed, x, z);
                    for (int y = -64; y <= 12; y++) putchar(mc_state(&w, x, y, z) != MC_AIR ? '1' : '0');
                    putchar('\n');
                }
            }
        }
        lysh_search_ctx_free(&ctx);
        return 0;
    }

    int sum_plain = footprint_sum(p2p, hut_x, hut_z, axis_z, NULL, 0);
    int sum_mine  = n_boxes > 0 ? footprint_sum(p2p, hut_x, hut_z, axis_z, boxes, n_boxes)
                                : sum_plain;

    int avg_plain = sum_plain / 63;
    int avg_mine  = sum_mine / 63;

    printf("seed %lld  hut (%d, %d)\n", seed, hut_x, hut_z);
    printf("  orientation : dir=%d  footprint %d(x) x %d(z)\n", dir,
           axis_z ? 7 : 9, axis_z ? 9 : 7);
    printf("  boxes       : %d\n", n_boxes);
    printf("  terrain     : sum=%d / 63  ->  avg_y = %d   (no mineshaft; == lysh avg_y)\n",
           sum_plain, avg_plain);
    printf("  mineshaft   : sum=%d / 63  ->  avg_y = %d\n", sum_mine, avg_mine);
    printf("  room worth  : %d\n", avg_plain - avg_mine);
    printf("MSROOM %lld %d %d %d %d %d\n", seed, hut_x, hut_z, avg_plain, avg_mine,
           avg_plain - avg_mine);

    if (guard && n_boxes > 0) {
        const int *b = boxes;
        int chunks = 0, tripped = 0, lava_total = 0, water_total = 0;
        for (int ccz = b[2] >> 4; ccz <= (b[5] >> 4); ccz++) {
            for (int ccx = b[0] >> 4; ccx <= (b[3] >> 4); ccx++) {
                int lava = 0, water = 0;
                guard_chunk(p2p, b, ccx, ccz, &lava, &water);
                chunks++;
                if (lava + water > 0) tripped++;
                lava_total += lava;
                water_total += water;
            }
        }
        printf("  guard       : %d of %d chunk(s) tripped  (lava %d, water %d on the expanded box faces)\n",
               tripped, chunks, lava_total, water_total);
        printf("GUARD %d %d %d %d %d\n", tripped > 0, chunks, tripped, lava_total, water_total);
    }

    lysh_search_ctx_free(&ctx);
    return 0;
}
