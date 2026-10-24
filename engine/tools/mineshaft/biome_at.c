#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "eval.h"
#include "phase1.h"
#include "phase2.h"
#include "biome.h"

static int g_y = 0;
static int const_height_cb(void *user, int x, int z) { (void)user; (void)x; (void)z; return g_y + 1; }

static const char *name_of(int id) {
    switch (id) {
        case 6: return "swamp"; case 174: return "dripstone_caves"; case 175: return "lush_caves"; case 183: return "deep_dark";
        case 177: return "meadow"; case 184: return "mangrove_swamp"; case 1: return "plains"; case 4: return "forest"; case 21: return "jungle"; case 2: return "desert";
        case 0: return "ocean"; case 24: return "deep_ocean"; case 7: return "river"; case 16: return "beach"; default: return "";
    }
}

int main(int argc, char **argv) {
    long long seed = 0; int cx = 0, cz = 0, have = 0, ys[16], ny = 0, ms = 0;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--seed") && i + 1 < argc) { seed = atoll(argv[++i]); have |= 1; }
        else if (!strcmp(argv[i], "--cx") && i + 1 < argc) { cx = atoi(argv[++i]); have |= 2; }
        else if (!strcmp(argv[i], "--cz") && i + 1 < argc) { cz = atoi(argv[++i]); have |= 4; }
        else if (!strcmp(argv[i], "--ms")) { ms = 1; }
        else if (!strcmp(argv[i], "--y") && i + 1 < argc && ny < 16) ys[ny++] = atoi(argv[++i]);
        else { fprintf(stderr, "biome_at: unknown arg %s\n", argv[i]); return 2; }
    }
    if (have != 7) { fprintf(stderr, "usage: biome_at --seed S --cx CX --cz CZ [--y Y ...]\n"); return 2; }
    if (!ny) { ys[ny++] = 50; ys[ny++] = -60; }
    lysh_phase1_opts opts; memset(&opts, 0, sizeof opts); opts.pre_26_2 = getenv("BIOME_26") ? 0 : 1;
    lysh_search_ctx ctx; lysh_search_ctx_init(&ctx, (uint64_t)seed, &opts, 0);
    for (int k = 0; k < ny; k++) {
        g_y = ys[k];
        lysh_biome_result br;
        lysh_swamp_hut_biome_ok(&ctx.biome, &ctx.p2.terrain, cx * 16, cz * 16 - (ms ? 8 : 0), const_height_cb, NULL, &br);
        printf("BIOME %d %s  y=%d  chunk (%d,%d) middle (%d,%d)  quart y %d\n", br.winner, name_of(br.winner), ys[k], cx, cz, cx * 16 + 8, cz * 16 + (ms ? 0 : 8), br.target.qy);
        if (getenv("BIOME_PARAMS")) printf("  target (x10000) temp %lld humid %lld cont %lld eros %lld depth %lld weird %lld  occ_y %d\n", br.target.t[0], br.target.t[1], br.target.t[2], br.target.t[3], br.target.t[4], br.target.t[5], br.target.occ_y);
    }
    lysh_search_ctx_free(&ctx);
    return 0;
}
