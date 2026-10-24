/* biome_map: biome ids for a rectangle of the overworld, for the map tab.
 *
 *   biome_map --seed S --x0 X --z0 Z --w W --h H [--step B] [--ss K] [--ver V] [--y Y]
 *
 * Writes W*H raw bytes to stdout, row by row (z outer, x inner). Each byte is a cubiomes biome id (swamp = 6).
 * Cell (i, j) covers the blocks [X + i*B, X + (i+1)*B) x [Z + j*B, Z + (j+1)*B); B is in blocks (default 16).
 * A sample is the biome at the middle of the chunk that holds the sample point, which is where the witch hut test looks.
 *
 *   --ss K   look at K x K sample points spread evenly over each cell (default 1: the cell's middle) and report the
 *            DOMINANT biome. A swamp wins when it fills at least a quarter of the cell, so swamps never vanish
 *            when the map is zoomed out. (Biome patches are 300-1500 blocks wide, so a cell of a kilometre or more
 *            is a mosaic whatever is done; this only keeps it honest about where the swamps are.)
 *
 * V is a game version name (1.18.2, 1.19.3, 1.20.1, 1.21.4, 26.3 ...; default 26.3): its own biome tree, the game's own, node for node.
 * Y (default 63) is the height the surface is assumed at; the depth parameter, and so the biome, depends on it slightly.
 *
 *   biome_map --seed S --stdin [--ver V] [--y Y]
 *
 * Reads "cx cz" chunk coordinates (one pair per line) from stdin and writes one byte per pair: the biome at that chunk's
 * middle. The app uses it to test the places a witch hut can generate. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif

#include "eval.h"
#include "phase1.h"
#include "phase2.h"
#include "biome.h"

#define SWAMP 6

static int g_y = 63;
static int const_height_cb(void *user, int x, int z) { (void)user; (void)x; (void)z; return g_y + 1; }

/* the hut test with the real surface: the game looks at the height of the chunk's middle column, not a fixed 63 */
static int real_height_cb(void *user, int x, int z) { return lysh_phase2_height_pre_carver((lysh_phase2 *)user, x, z); }

/* The biome the hut test sees at a chunk. The real height costs about 0.5 ms a chunk, so first try six heights that
 * cover where a swamp's surface can be (a fixed 63 once missed one on a low surface): if they all say swamp, or none does, that is the answer; only a chunk where they
 * disagree (a swamp's edge) pays for the real height. Checked against the real height on 278,000 candidates: no difference. */
static int hut_biome(lysh_search_ctx *ctx, long long cx, long long cz) {
    static const int ys[] = {40, 48, 56, 64, 72, 80};
    int swamps = 0, first = 1;
    for (int i = 0; i < 6; i++) {
        lysh_biome_result br;
        g_y = ys[i];
        lysh_swamp_hut_biome_ok(&ctx->biome, &ctx->p2.terrain, (int)(cx * 16), (int)(cz * 16), const_height_cb, NULL, &br);
        if (i == 3) first = br.winner & 0xFF;                 /* the biome at y 64, for the answer when it isn't a swamp */
        swamps += (br.winner & 0xFF) == SWAMP;
    }
    if (swamps == 0 || swamps == 6) return swamps ? SWAMP : first;
    lysh_biome_result br;
    lysh_swamp_hut_biome_ok(&ctx->biome, &ctx->p2.terrain, (int)(cx * 16), (int)(cz * 16), real_height_cb, &ctx->p2, &br);
    return br.winner & 0xFF;
}

static long long floor_div(long long a, long long b) { long long q = a / b; return (a % b != 0 && ((a < 0) != (b < 0))) ? q - 1 : q; }

int main(int argc, char **argv) {
    long long seed = 0, x0 = 0, z0 = 0;
    int w = 0, h = 0, step = 16, have = 0, from_stdin = 0, ss = 1;
    const char *ver = "26.3";
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--seed") && i + 1 < argc) { seed = atoll(argv[++i]); have |= 1; }
        else if (!strcmp(argv[i], "--x0") && i + 1 < argc) { x0 = atoll(argv[++i]); have |= 2; }
        else if (!strcmp(argv[i], "--z0") && i + 1 < argc) { z0 = atoll(argv[++i]); have |= 4; }
        else if (!strcmp(argv[i], "--w") && i + 1 < argc) { w = atoi(argv[++i]); have |= 8; }
        else if (!strcmp(argv[i], "--h") && i + 1 < argc) { h = atoi(argv[++i]); have |= 16; }
        else if (!strcmp(argv[i], "--step") && i + 1 < argc) step = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--ss") && i + 1 < argc) ss = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--ver") && i + 1 < argc) ver = argv[++i];
        else if (!strcmp(argv[i], "--y") && i + 1 < argc) g_y = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--stdin")) { from_stdin = 1; have |= 30; }
        else { fprintf(stderr, "biome_map: unknown arg %s\n", argv[i]); return 2; }
    }
    if (from_stdin) { w = h = 1; }
    if (have != 31 || w <= 0 || h <= 0 || w > 4096 || h > 4096 || step < 1 || ss < 1 || ss > 4) {
        fprintf(stderr, "usage: biome_map --seed S (--x0 X --z0 Z --w W --h H [--step B] [--ss 1..4] | --stdin) "
                        "[--ver 1.18.2|1.19.3|...|26.3] [--y Y]\n");
        return 2;
    }
    int tree = 0, is118 = 0, pre262 = 0;
    if (lysh_biome_version(ver, &tree, &is118, &pre262)) { fprintf(stderr, "biome_map: --ver must be a game version such as 1.18.2, 1.19.3, 1.21.4 or 26.3\n"); return 2; }

    lysh_phase1_opts opts; memset(&opts, 0, sizeof opts);
    opts.mc_1_18_2 = is118;
    opts.pre_26_2 = pre262;
    opts.biome_tree = tree + 1;
    lysh_search_ctx ctx; lysh_search_ctx_init(&ctx, (uint64_t)seed, &opts, 0);

#ifdef _WIN32
    _setmode(_fileno(stdout), _O_BINARY);
    _setmode(_fileno(stdin), _O_BINARY);
#endif
    if (from_stdin) {
        long long cx, cz;
        while (scanf("%lld %lld", &cx, &cz) == 2) putchar(hut_biome(&ctx, cx, cz));
        lysh_search_ctx_free(&ctx);
        return 0;
    }
    unsigned char *row = (unsigned char *)malloc((size_t)w);
    if (!row) return 1;
    int n = ss * ss;
    for (int j = 0; j < h; j++) {
        for (int i = 0; i < w; i++) {
            int cnt[256]; memset(cnt, 0, sizeof cnt);
            int best = -1, bestn = 0;
            for (int b = 0; b < ss; b++) {
                long long Z = z0 + (long long)j * step + ((2LL * b + 1) * step) / (2LL * ss);
                for (int a = 0; a < ss; a++) {
                    long long X = x0 + (long long)i * step + ((2LL * a + 1) * step) / (2LL * ss);
                    int cx = (int)floor_div(X, 16), cz = (int)floor_div(Z, 16);
                    lysh_biome_result br;
                    /* the call snaps to the chunk it is given and samples that chunk's middle (+8, +8), where the hut test looks */
                    lysh_swamp_hut_biome_ok(&ctx.biome, &ctx.p2.terrain, cx * 16, cz * 16, const_height_cb, NULL, &br);
                    int id = br.winner & 0xFF;
                    if (++cnt[id] > bestn) { bestn = cnt[id]; best = id; }
                }
            }
            if (cnt[SWAMP] * 4 >= n && cnt[SWAMP] > 0) best = SWAMP;       /* swamp wins at a quarter of the cell */
            row[i] = (unsigned char)best;
        }
        if (fwrite(row, 1, (size_t)w, stdout) != (size_t)w) { free(row); return 1; }
    }
    free(row);
    lysh_search_ctx_free(&ctx);
    return 0;
}
