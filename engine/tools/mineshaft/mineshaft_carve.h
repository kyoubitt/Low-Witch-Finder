#ifndef MINESHAFT_CARVE_H
#define MINESHAFT_CARVE_H
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define MC_Y0 (-64)
#define MC_YN 140
static float mc_dome_eps = 1.05f;
static int mc_off_x = 0, mc_off_z = 0;
static int mc_guard_off = 0;
static int mc_cur_idx = -1; static int mc_trace_x = 0x7fffffff, mc_trace_y = 0, mc_trace_z = 0;
static int mc_noplanks = 0;
#define MC_AIR 1

typedef struct { char kind; int x0, y0, z0, x1, y1, z1; int facing; int parent; int rails; } McPiece;
typedef int (*McNatural)(void* ctx, int x, int y, int z);

typedef struct {
    int cx0, cz0;
    uint8_t carved[16][16][MC_YN];
    uint8_t placed[16][16][MC_YN];
    uint8_t maxPl[16][16];
    McNatural nat; void* ctx;
    int topLayer;
    uint64_t rng;
    int refused;
    int refusedMask;
} McWorld;

static void mc_init(McWorld* w, int cx0, int cz0, McNatural nat, void* ctx, int topLayer) {
    memset(w, 0, sizeof *w);
    w->cx0 = cx0; w->cz0 = cz0; w->nat = nat; w->ctx = ctx; w->topLayer = topLayer;
}

static int mc_fast = 0;
static int (*mc_nattop_hint)(void* ctx, int x, int z) = 0;

static unsigned char* mc_natc = 0;

static int mc_state(const McWorld* w, int x, int y, int z) {
    const int lx = x - w->cx0, lz = z - w->cz0, ly = y - MC_Y0;
    if (lx >= 0 && lx < 16 && lz >= 0 && lz < 16 && ly >= 0 && ly < MC_YN) {
        if (w->placed[lx][lz][ly] == 1) return 0;
        if (w->placed[lx][lz][ly] == 2) return 4;
        if (w->carved[lx][lz][ly]) return MC_AIR;
        if (mc_natc) {
            unsigned char* c = &mc_natc[((lx * 16) + lz) * MC_YN + ly];
            if (!*c) *c = (unsigned char)(w->nat(w->ctx, x, y, z) + 1);
            return *c - 1;
        }
    }
    return w->nat(w->ctx, x, y, z);
}

static void mc_fill(McWorld* w, int x0, int y0, int z0, int x1, int y1, int z1) {
    if (x0 < w->cx0) x0 = w->cx0;
    if (z0 < w->cz0) z0 = w->cz0;
    if (x1 > w->cx0 + 15) x1 = w->cx0 + 15;
    if (z1 > w->cz0 + 15) z1 = w->cz0 + 15;
    if (y0 < MC_Y0) y0 = MC_Y0;
    if (y1 > MC_Y0 + MC_YN - 1) y1 = MC_Y0 + MC_YN - 1;
    for (int x = x0; x <= x1; x++) for (int z = z0; z <= z1; z++) for (int y = y0; y <= y1; y++) {
        w->carved[x - w->cx0][z - w->cz0][y - MC_Y0] = 1;
        w->placed[x - w->cx0][z - w->cz0][y - MC_Y0] = 0;
    }
}

static void mc_place(McWorld* w, int x, int y, int z) {
    if (x == mc_trace_x && y == mc_trace_y && z == mc_trace_z) fprintf(stderr, "TRACE place (%d,%d,%d) by piece %d\n", x, y, z, mc_cur_idx);
    const int lx = x - w->cx0, lz = z - w->cz0, ly = y - MC_Y0;
    if (lx < 0 || lx > 15 || lz < 0 || lz > 15 || ly < 0 || ly >= MC_YN) return;
    w->placed[lx][lz][ly] = 1; w->carved[lx][lz][ly] = 0;
    if (ly + 1 > w->maxPl[lx][lz]) w->maxPl[lx][lz] = (uint8_t)(ly + 1);
}

static int mc_isLiquid(const McWorld* w, int x, int y, int z) { const int s = mc_state(w, x, y, z); return s == 2 || s == 3; }

static int mc_guard(const McWorld* w, int bx0, int by0, int bz0, int bx1, int by1, int bz1) {
    const int i = bx0 - 1 > w->cx0 ? bx0 - 1 : w->cx0;
    const int j = by0 - 1 > -64 ? by0 - 1 : -64;
    const int k = bz0 - 1 > w->cz0 ? bz0 - 1 : w->cz0;
    const int l = bx1 + 1 < w->cx0 + 15 ? bx1 + 1 : w->cx0 + 15;
    const int m = by1 + 1 < 319 ? by1 + 1 : 319;
    const int n = bz1 + 1 < w->cz0 + 15 ? bz1 + 1 : w->cz0 + 15;
    for (int o = i; o <= l; o++) for (int p = k; p <= n; p++) { if (mc_isLiquid(w, o, j, p) || mc_isLiquid(w, o, m, p)) return 1; }
    for (int o = i; o <= l; o++) for (int p = j; p <= m; p++) { if (mc_isLiquid(w, o, p, k) || mc_isLiquid(w, o, p, n)) return 1; }
    for (int o = k; o <= n; o++) for (int p = j; p <= m; p++) { if (mc_isLiquid(w, i, p, o) || mc_isLiquid(w, l, p, o)) return 1; }
    return 0;
}

static int mc_solidFrom(const McWorld* w, int x, int y, int z) {
    int yEnd = MC_Y0 + MC_YN - 1;
    if (mc_fast && mc_nattop_hint) {
        const int lx = x - w->cx0, lz = z - w->cz0;
        if (lx >= 0 && lx < 16 && lz >= 0 && lz < 16) {
            int top = mc_nattop_hint(w->ctx, x, z); const int pl = MC_Y0 + (int)w->maxPl[lx][lz] - 1; if (pl > top) top = pl;
            if (top < yEnd) yEnd = top;
        }
    }
    for (int yy = y < MC_Y0 ? MC_Y0 : y; yy <= yEnd; yy++) if (mc_state(w, x, yy, z) == 0) return 1;
    return 0;
}

static void mc_floorFill(McWorld* w, const McPiece* p) {
    if (mc_noplanks & 1) return;
    for (int x = p->x0; x <= p->x1; x++) for (int z = p->z0; z <= p->z1; z++) {
        if (x < w->cx0 || x > w->cx0 + 15 || z < w->cz0 || z > w->cz0 + 15) continue;
        if (x == mc_trace_x && p->y0 - 1 == mc_trace_y && z == mc_trace_z) {
            int fy = 9999; for (int yy = p->y0; yy < MC_Y0 + MC_YN; yy++) if (mc_state(w, x, yy, z) == 0) { fy = yy; break; }
            fprintf(stderr, "TRACE floor piece %d (%c y0 %d): state below %d, first solid at/above y0: %d\n", mc_cur_idx, p->kind, p->y0, mc_state(w, x, p->y0 - 1, z), fy);
        }
        if (mc_state(w, x, p->y0 - 1, z) == MC_AIR && mc_solidFrom(w, x, p->y0, z)) mc_place(w, x, p->y0 - 1, z);
    }
}

static void mc_rails(McWorld* w, const McPiece* p) {
    if (!p->rails || w->topLayer == 1) return;
    const int alongZ = (p->x1 - p->x0) == 2;
    const int n = alongZ ? p->z1 - p->z0 + 1 : p->x1 - p->x0 + 1;
    for (int i = 0; i < n; i++) {
        const int x = alongZ ? p->x0 + 1 : p->x0 + i, z = alongZ ? p->z0 + i : p->z0 + 1;
        if (x < w->cx0 || x > w->cx0 + 15 || z < w->cz0 || z > w->cz0 + 15) continue;
        if (mc_state(w, x, p->y0 - 1, z) != 0) continue;
        if (w->topLayer == 2) {
            const double chance = mc_solidFrom(w, x, p->y0 + 1, z) ? 0.7 : 0.9;
            w->rng ^= w->rng << 13; w->rng ^= w->rng >> 7; w->rng ^= w->rng << 17;
            if (!((double)(w->rng >> 11) * (1.0 / 9007199254740992.0) < chance)) continue;
        }
        if (mc_state(w, x, p->y0, z) == MC_AIR) { w->placed[x - w->cx0][z - w->cz0][p->y0 - MC_Y0] = 2; if (p->y0 - MC_Y0 + 1 > w->maxPl[x - w->cx0][z - w->cz0]) w->maxPl[x - w->cx0][z - w->cz0] = (uint8_t)(p->y0 - MC_Y0 + 1); }
    }
}

static void mc_apply(McWorld* w, const McPiece* pc, int n) {
    for (int idx = 0; idx < n; idx++) {
        const McPiece* p = &pc[idx];
        mc_cur_idx = idx;
        if (p->x1 < w->cx0 || p->x0 > w->cx0 + 15 || p->z1 < w->cz0 || p->z0 > w->cz0 + 15) continue;
        if (!mc_guard_off && mc_guard(w, p->x0, p->y0, p->z0, p->x1, p->y1, p->z1)) {
            w->refused++; if (idx < 32) w->refusedMask |= 1 << idx;
            continue;
        }
        switch (p->kind) {
        case 'R': {
            const int top3 = p->y0 + 3 < p->y1 ? p->y0 + 3 : p->y1;
            mc_fill(w, p->x0, p->y0 + 1, p->z0, p->x1, top3, p->z1);
            for (int c = 1; c < n; c++) {
                const McPiece* ch = &pc[c];
                if (ch->parent != 0) continue;
                int ex0 = ch->x0, ex1 = ch->x1, ez0 = ch->z0, ez1 = ch->z1;
                if (ch->facing == 0)      { ez0 = p->z0;     ez1 = p->z0 + 1; }
                else if (ch->facing == 1) { ez0 = p->z1 - 1; ez1 = p->z1; }
                else if (ch->facing == 2) { ex0 = p->x0;     ex1 = p->x0 + 1; }
                else                      { ex0 = p->x1 - 1; ex1 = p->x1; }
                mc_fill(w, ex0, ch->y1 - 2, ez0, ex1, ch->y1, ez1);
            }
            {
                const float W = (float)(p->x1 - p->x0 + 1), H = (float)(p->y1 - (p->y0 + 4) + 1), D = (float)(p->z1 - p->z0 + 1);
                const float cx = (float)(p->x0 + mc_off_x) + W / 2.0f, cz = (float)(p->z0 + mc_off_z) + D / 2.0f;
                for (int y = p->y0 + 4; y <= p->y1; y++) {
                    const float dy = (float)(y - (p->y0 + 4)) / H;
                    for (int x = p->x0; x <= p->x1; x++) {
                        const float dx = ((float)(x + mc_off_x) - cx) / (W * 0.5f);
                        for (int z = p->z0; z <= p->z1; z++) {
                            const float dz = ((float)(z + mc_off_z) - cz) / (D * 0.5f);
                            if (dx * dx + dy * dy + dz * dz <= mc_dome_eps) mc_fill(w, x, y, z, x, y, z);
                        }
                    }
                }
            }
            break;
        }
        case 'C':
            mc_fill(w, p->x0, p->y0, p->z0, p->x1, p->y0 + 1, p->z1);
            if (w->topLayer == 2) {
                for (int x = p->x0; x <= p->x1; x++) for (int z = p->z0; z <= p->z1; z++) {
                    w->rng ^= w->rng << 13; w->rng ^= w->rng >> 7; w->rng ^= w->rng << 17;
                    if ((double)(w->rng >> 11) * (1.0 / 9007199254740992.0) < 0.8) mc_fill(w, x, p->y0 + 2, z, x, p->y0 + 2, z);
                }
            } else if (w->topLayer) mc_fill(w, p->x0, p->y0 + 2, p->z0, p->x1, p->y0 + 2, p->z1);
            mc_floorFill(w, p);
            mc_rails(w, p);
            break;
        case 'X': case 'Y': {
            if (p->kind == 'Y') {
                mc_fill(w, p->x0 + 1, p->y0, p->z0, p->x1 - 1, p->y0 + 2, p->z1);
                mc_fill(w, p->x0, p->y0, p->z0 + 1, p->x1, p->y0 + 2, p->z1 - 1);
                mc_fill(w, p->x0 + 1, p->y1 - 2, p->z0, p->x1 - 1, p->y1, p->z1);
                mc_fill(w, p->x0, p->y1 - 2, p->z0 + 1, p->x1, p->y1, p->z1 - 1);
                mc_fill(w, p->x0 + 1, p->y0 + 3, p->z0 + 1, p->x1 - 1, p->y0 + 3, p->z1 - 1);
            } else {
                mc_fill(w, p->x0 + 1, p->y0, p->z0, p->x1 - 1, p->y1, p->z1);
                mc_fill(w, p->x0, p->y0, p->z0 + 1, p->x1, p->y1, p->z1 - 1);
            }
            const int px[4] = { p->x0 + 1, p->x0 + 1, p->x1 - 1, p->x1 - 1 };
            const int pz[4] = { p->z0 + 1, p->z1 - 1, p->z0 + 1, p->z1 - 1 };
            for (int q = 0; q < 4; q++) {
#ifndef MC_SLOW

                if (px[q] < w->cx0 || px[q] > w->cx0 + 15 || pz[q] < w->cz0 || pz[q] > w->cz0 + 15) continue;
#endif
                if (!(mc_noplanks & 2) && mc_state(w, px[q], p->y1 + 1, pz[q]) != MC_AIR)
                    for (int y = p->y0; y <= p->y1; y++) mc_place(w, px[q], y, pz[q]);
            }
            mc_floorFill(w, p);
            break;
        }
        case 'S': {
            static const int lo[9] = { 5, 5, 4, 3, 2, 1, 1, 0, 0 }, hi[9] = { 7, 7, 7, 6, 5, 4, 3, 2, 2 };
            for (int lz = 0; lz < 9; lz++) {
                int zz, xx0 = p->x0, xx1 = p->x1, zz0 = 0, zz1 = 0;
                if (p->facing == 0)      { zz0 = zz1 = p->z1 - lz; xx0 = p->x0; xx1 = p->x1; }
                else if (p->facing == 1) { zz0 = zz1 = p->z0 + lz; xx0 = p->x0; xx1 = p->x1; }
                else if (p->facing == 2) { xx0 = xx1 = p->x1 - lz; zz0 = p->z0; zz1 = p->z1; }
                else                     { xx0 = xx1 = p->x0 + lz; zz0 = p->z0; zz1 = p->z1; }
                (void)zz;
                mc_fill(w, xx0, p->y0 + lo[lz], zz0, xx1, p->y0 + hi[lz], zz1);
            }
            break;
        }
        default: break;
        }
    }
}

static int mc_height(const McWorld* w, int x, int z) {
    int ys = 319;
    if (mc_fast && mc_nattop_hint) {
        const int lx = x - w->cx0, lz = z - w->cz0;
        if (lx >= 0 && lx < 16 && lz >= 0 && lz < 16) {
            int top = mc_nattop_hint(w->ctx, x, z); const int pl = MC_Y0 + (int)w->maxPl[lx][lz] - 1; if (pl > top) top = pl;
            if (top < ys) ys = top;
        }
    }
    for (int y = ys; y >= -64; y--) if (mc_state(w, x, y, z) != MC_AIR) return y + 1;
    return -64;
}
#endif
