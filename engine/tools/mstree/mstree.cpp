#define MT_RECORD_META 1
#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <climits>
#include "mineshaft_tree.h"
#include "mineshaft_tree_iter.h"
#include "swamphut.h"

static bool startPasses(uint64_t X) {
    uint64_t s = (X ^ MT_MULT) & MT_MASK;
    s = (s * MT_MULT + 0xBULL) & MT_MASK;  const uint64_t a = s >> 22;
    s = (s * MT_MULT + 0xBULL) & MT_MASK;  const uint64_t b = s >> 21;
    return (double)((a << 27) + b) * 1.1102230246251565e-16 < 0.004;
}

static int emit(uint64_t X, int cx, int cz, int parentOverride, bool own) {
    static MtTree t; static MtRun run;
    mtSeed(t, X);
    mtRunInit(t, run, 0, 0, INT_MIN, 0x7fffffff);
    while (mtRunStep(t, run)) {}
    MtResult r; mtRunFinish(t, &r);
    const int m = r.roomFloor - 50;
    for (int i = 0; i < t.n; i++) {
        const uint32_t w = t.pk[i], v = t.py[i];
        const int x0 = (int8_t)w + t.sx + cx * 16, z0 = (int8_t)(w >> 8) + t.sz + cz * 16;
        const int x1 = (int8_t)(w >> 16) + t.sx + cx * 16, z1 = (int8_t)(w >> 24) + t.sz + cz * 16;
        const int y0 = (int8_t)v + 50 + m, y1 = (int8_t)(v >> 8) + 50 + m;
        const int type = t.meta[i] & 3, facing = (int)((t.meta[i] >> 2) & 7) - 1;
        const char kind = type == MT_ROOM ? 'R' : type == MT_CORRIDOR ? 'C' : type == MT_CROSSING ? (y1 - y0 + 1 > 3 ? 'Y' : 'X') : 'S';
        printf("%c %d %d %d %d %d %d %d %d %d\n", kind, x0, y0, z0, x1, y1, z1, facing, own ? t.parent[i] : parentOverride, (t.meta[i] >> 5) & 1);
    }
    return r.roomFloor;
}

int main(int argc, char** argv) {
    if (argc < 4) { fprintf(stderr, "usage: mstree <worldSeed> <chunkX> <chunkZ> [R]\n"); return 2; }
    const int64_t ws = strtoll(argv[1], nullptr, 10);
    const int cx = atoi(argv[2]), cz = atoi(argv[3]);
    const int R = argc > 4 ? atoi(argv[4]) : 0;
    const int64_t s48 = ws & 0xFFFFFFFFFFFFLL;
    JavaRandom jr; jr.setSeed(s48);
    const int64_t a = javaNextLong(jr), b = javaNextLong(jr);
    const uint64_t X = (uint64_t)(((int64_t)cx * a) ^ ((int64_t)cz * b) ^ s48) & MT_MASK;
    if (!startPasses(X)) { fprintf(stderr, "chunk (%d,%d) is not a mineshaft start\n", cx, cz); return 3; }
    const int floor0 = emit(X, cx, cz, 0, true);
    fprintf(stderr, "room floor %d\n", floor0);
    for (int dx = -R; dx <= R; dx++) for (int dz = -R; dz <= R; dz++) {
        if (!dx && !dz) continue;
        const uint64_t X2 = (uint64_t)(((int64_t)(cx + dx) * a) ^ ((int64_t)(cz + dz) * b) ^ s48) & MT_MASK;
        if (startPasses(X2)) emit(X2, cx + dx, cz + dz, -2, false);
    }
    return 0;
}
