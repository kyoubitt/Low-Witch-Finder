#pragma once
#include <cstdint>

#ifdef __CUDACC__
#define MT_HD __host__ __device__
#else
#define MT_HD
#endif

#define MT_MASK 0xFFFFFFFFFFFFULL
#define MT_MULT 0x5DEECE66DULL
#ifndef MT_MAXP
#define MT_MAXP 512
#endif

#ifdef MT_SHARED
#define MT_OVL2 1
#ifndef MT_SH_CAP
#define MT_SH_CAP 32
#endif
#ifndef MT_SH_BLOCK
#define MT_SH_BLOCK 128
#endif
__shared__ uint32_t g_shPk[MT_SH_CAP * MT_SH_BLOCK];
__shared__ uint16_t g_shPy[MT_SH_CAP * MT_SH_BLOCK];
#endif

enum { MT_N = 0, MT_S = 1, MT_W = 2, MT_E = 3 };
enum { MT_ROOM = 0, MT_CORRIDOR = 1, MT_CROSSING = 2, MT_STAIRS = 3 };

struct MtPiece {
    int32_t x0, y0, z0, x1, y1, z1;
    int8_t type, facing, depth, rails;
};

struct MtTree {
    uint64_t rs;

#ifdef MT_OVL2
    alignas(16) uint32_t pk[MT_MAXP];
#else
    uint32_t pk[MT_MAXP];
#endif
    uint16_t py[MT_MAXP];
#ifdef MT_OVL2_BLK
#ifdef MT_OVL2_BLK2
    uint32_t bw2[MT_MAXP / 64];
#endif
#ifndef MT_BS
#define MT_BS 3
#endif
    uint32_t bw[MT_MAXP >> MT_BS];
#endif
    int minY, maxY;
    int rangeBad;
#ifdef MT_RECORD_META
    uint8_t meta[MT_MAXP];
    int16_t parent[MT_MAXP];
#endif
    int n;
    int sx, sz;
    int abortBelow;
    int minRoomTop;
    int budget;
    int aborted;
    int overflow;
    int deferred;
#ifdef MT_DIAG_PROFILE
    long long ovlCyc; long long ovlCalls; long long ovlScan;
#endif
};

MT_HD inline int32_t mtNext(MtTree& t, int bits) {
    t.rs = (t.rs * MT_MULT + 0xBULL) & MT_MASK;
    return (int32_t)(t.rs >> (48 - bits));
}
MT_HD inline int32_t mtNextInt(MtTree& t, int32_t bound) {
    if ((bound & -bound) == bound) return (int32_t)(((int64_t)bound * (int64_t)mtNext(t, 31)) >> 31);
    int32_t bits, val;
    do {
        bits = mtNext(t, 31);
        val = bits % bound;
    } while ((int32_t)((uint32_t)bits - (uint32_t)val + (uint32_t)(bound - 1)) < 0);
    return val;
}
MT_HD inline bool mtNextBool(MtTree& t) { return mtNext(t, 1) != 0; }

#ifndef MT_OVL_UNROLL
#define MT_OVL_UNROLL 4
#endif
MT_HD inline bool mtFits8(int v) { return v >= -128 && v <= 127; }

#ifdef MT_OVL2_BLK
MT_HD inline uint32_t mtMin4(uint32_t a, uint32_t b) {
#if defined(__CUDA_ARCH__)
    return __vminu4(a, b);
#else
    uint32_t r = 0;
    for (int s = 0; s < 32; s += 8) { const uint32_t x = (a >> s) & 255, y = (b >> s) & 255; r |= (x < y ? x : y) << s; }
    return r;
#endif
}
#endif

MT_HD inline void mtPush(MtTree& t, const MtPiece& a) {
    const int rx0 = a.x0 - t.sx, rz0 = a.z0 - t.sz, rx1 = a.x1 - t.sx, rz1 = a.z1 - t.sz;
    const int ry0 = a.y0 - 50, ry1 = a.y1 - 50;
    if (!(mtFits8(rx0) & mtFits8(rz0) & mtFits8(rx1) & mtFits8(rz1) & mtFits8(ry0) & mtFits8(ry1)))
        t.rangeBad = 1;
#if defined(MT_SHARED) && defined(__CUDA_ARCH__)
    {
        const uint32_t wpk = (uint32_t)((rx0 + 128) & 255) | ((uint32_t)((rz0 + 128) & 255) << 8) | ((uint32_t)((127 - rx1) & 255) << 16) | ((uint32_t)((127 - rz1) & 255) << 24);
        const uint16_t wpy = (uint16_t)((ry0 & 255) | ((ry1 & 255) << 8));
        if (t.n < MT_SH_CAP) { g_shPk[t.n * MT_SH_BLOCK + threadIdx.x] = wpk; g_shPy[t.n * MT_SH_BLOCK + threadIdx.x] = wpy; }
        else { t.pk[t.n] = wpk; t.py[t.n] = wpy; }
    }
#elif defined(MT_OVL2)
    {
        const uint32_t wpk = (uint32_t)((rx0 + 128) & 255) | ((uint32_t)((rz0 + 128) & 255) << 8)
                           | ((uint32_t)((127 - rx1) & 255) << 16) | ((uint32_t)((127 - rz1) & 255) << 24);
        t.pk[t.n] = wpk;
#ifdef MT_OVL2_BLK
        t.bw[t.n >> MT_BS] = (t.n & ((1 << MT_BS) - 1)) ? mtMin4(t.bw[t.n >> MT_BS], wpk) : wpk;
#ifdef MT_OVL2_BLK2
        t.bw2[t.n >> 6] = (t.n & 63) ? mtMin4(t.bw2[t.n >> 6], wpk) : wpk;
#endif
#endif
    }
#else
    t.pk[t.n] = (uint32_t)(rx0 & 255) | ((uint32_t)(rz0 & 255) << 8)
              | ((uint32_t)(rx1 & 255) << 16) | ((uint32_t)(rz1 & 255) << 24);
#endif
#if !(defined(MT_SHARED) && defined(__CUDA_ARCH__))
    t.py[t.n] = (uint16_t)((ry0 & 255) | ((ry1 & 255) << 8));
#endif
    if (a.y0 < t.minY) t.minY = a.y0;
    if (a.y1 > t.maxY) t.maxY = a.y1;
    t.n++;
}

#ifdef MT_OVL2

MT_HD inline bool mtLe4(uint32_t W, uint32_t Q) {
#if defined(__CUDA_ARCH__)
    return __vcmpleu4(W, Q) == 0xFFFFFFFFu;
#else
    return ((W & 255) <= (Q & 255)) & (((W >> 8) & 255) <= ((Q >> 8) & 255)) & (((W >> 16) & 255) <= ((Q >> 16) & 255)) & ((W >> 24) <= (Q >> 24));
#endif
}
MT_HD inline bool mtYov(uint32_t v, int qy0, int qy1) { return (int)(int8_t)(v >> 8) >= qy0 && (int)(int8_t)v <= qy1; }

MT_HD inline bool mtOverlapsImpl(MtTree& t, int x0, int y0, int z0, int x1, int y1, int z1) {
    const int qx0 = x0 - t.sx, qz0 = z0 - t.sz, qx1 = x1 - t.sx, qz1 = z1 - t.sz;
    const int qy0 = y0 - 50, qy1 = y1 - 50;
    if (!(mtFits8(qx0) & mtFits8(qz0) & mtFits8(qx1) & mtFits8(qz1) & mtFits8(qy0) & mtFits8(qy1))) {
        t.rangeBad = 1;
        return false;
    }
    const uint32_t Q = (uint32_t)(qx1 + 128) | ((uint32_t)(qz1 + 128) << 8) | ((uint32_t)(127 - qx0) << 16) | ((uint32_t)(127 - qz0) << 24);
    int i = t.n;
#if defined(MT_SHARED) && defined(__CUDA_ARCH__)
    {
        const int lo = MT_SH_CAP;
        while (i > lo && (i & 3)) { --i; if (mtLe4(t.pk[i], Q) && mtYov(t.py[i], qy0, qy1)) return true; }
        for (; i > lo; i -= 4) {
            const uint4 w = *reinterpret_cast<const uint4*>(&t.pk[i - 4]);
            const bool a3 = mtLe4(w.w, Q), a2 = mtLe4(w.z, Q), a1 = mtLe4(w.y, Q), a0 = mtLe4(w.x, Q);
            if (a3 | a2 | a1 | a0) {
                if (a3 && mtYov(t.py[i - 1], qy0, qy1)) return true;
                if (a2 && mtYov(t.py[i - 2], qy0, qy1)) return true;
                if (a1 && mtYov(t.py[i - 3], qy0, qy1)) return true;
                if (a0 && mtYov(t.py[i - 4], qy0, qy1)) return true;
            }
        }
        const int tx = threadIdx.x;
        while (i & 3) { --i; if (mtLe4(g_shPk[i * MT_SH_BLOCK + tx], Q) && mtYov(g_shPy[i * MT_SH_BLOCK + tx], qy0, qy1)) return true; }
        for (; i > 0; i -= 4) {
            const uint32_t w3 = g_shPk[(i - 1) * MT_SH_BLOCK + tx], w2 = g_shPk[(i - 2) * MT_SH_BLOCK + tx], w1 = g_shPk[(i - 3) * MT_SH_BLOCK + tx], w0 = g_shPk[(i - 4) * MT_SH_BLOCK + tx];
            const bool a3 = mtLe4(w3, Q), a2 = mtLe4(w2, Q), a1 = mtLe4(w1, Q), a0 = mtLe4(w0, Q);
            if (a3 | a2 | a1 | a0) {
                if (a3 && mtYov(g_shPy[(i - 1) * MT_SH_BLOCK + tx], qy0, qy1)) return true;
                if (a2 && mtYov(g_shPy[(i - 2) * MT_SH_BLOCK + tx], qy0, qy1)) return true;
                if (a1 && mtYov(g_shPy[(i - 3) * MT_SH_BLOCK + tx], qy0, qy1)) return true;
                if (a0 && mtYov(g_shPy[(i - 4) * MT_SH_BLOCK + tx], qy0, qy1)) return true;
            }
        }
        return false;
    }
#endif
#ifdef MT_OVL2_BLK
    {

        int b = (i - 1) >> MT_BS;
        bool partial = (i & ((1 << MT_BS) - 1)) != 0;
        while (b >= 0) {
            if (mtLe4(t.bw[b], Q)) {
                if (partial) {
                    for (int e = i - 1; e >= (b << MT_BS); --e) if (mtLe4(t.pk[e], Q) && mtYov(t.py[e], qy0, qy1)) return true;
                } else {
                    const int base = b << MT_BS;
#pragma unroll
                    for (int g = (1 << MT_BS) / 4 - 1; g >= 0; --g) {
#if defined(__CUDA_ARCH__)
                        const uint4 w = *reinterpret_cast<const uint4*>(&t.pk[base + 4 * g]);
                        const uint32_t v0 = w.x, v1 = w.y, v2 = w.z, v3 = w.w;
#else
                        const uint32_t v0 = t.pk[base + 4 * g], v1 = t.pk[base + 4 * g + 1], v2 = t.pk[base + 4 * g + 2], v3 = t.pk[base + 4 * g + 3];
#endif
                        const bool a3 = mtLe4(v3, Q), a2 = mtLe4(v2, Q), a1 = mtLe4(v1, Q), a0 = mtLe4(v0, Q);
                        if (a3 | a2 | a1 | a0) {
                            if (a3 && mtYov(t.py[base + 4 * g + 3], qy0, qy1)) return true;
                            if (a2 && mtYov(t.py[base + 4 * g + 2], qy0, qy1)) return true;
                            if (a1 && mtYov(t.py[base + 4 * g + 1], qy0, qy1)) return true;
                            if (a0 && mtYov(t.py[base + 4 * g], qy0, qy1)) return true;
                        }
                    }
                }
            }
            partial = false; --b;
        }
        return false;
    }
#endif
#ifdef MT_OVL2_U8

    while (i & 3) { --i; if (mtLe4(t.pk[i], Q) && mtYov(t.py[i], qy0, qy1)) return true; }
    if (i & 4) {
        const uint4 w = *reinterpret_cast<const uint4*>(&t.pk[i - 4]);
        const bool a3 = mtLe4(w.w, Q), a2 = mtLe4(w.z, Q), a1 = mtLe4(w.y, Q), a0 = mtLe4(w.x, Q);
        if (a3 | a2 | a1 | a0) {
            if (a3 && mtYov(t.py[i - 1], qy0, qy1)) return true;
            if (a2 && mtYov(t.py[i - 2], qy0, qy1)) return true;
            if (a1 && mtYov(t.py[i - 3], qy0, qy1)) return true;
            if (a0 && mtYov(t.py[i - 4], qy0, qy1)) return true;
        }
        i -= 4;
    }
    for (; i > 0; i -= 8) {
        const uint4 wb = *reinterpret_cast<const uint4*>(&t.pk[i - 4]);
        const uint4 wa = *reinterpret_cast<const uint4*>(&t.pk[i - 8]);
        const bool b3 = mtLe4(wb.w, Q), b2 = mtLe4(wb.z, Q), b1 = mtLe4(wb.y, Q), b0 = mtLe4(wb.x, Q);
        const bool a3 = mtLe4(wa.w, Q), a2 = mtLe4(wa.z, Q), a1 = mtLe4(wa.y, Q), a0 = mtLe4(wa.x, Q);
        if (b3 | b2 | b1 | b0 | a3 | a2 | a1 | a0) {
            if (b3 && mtYov(t.py[i - 1], qy0, qy1)) return true;
            if (b2 && mtYov(t.py[i - 2], qy0, qy1)) return true;
            if (b1 && mtYov(t.py[i - 3], qy0, qy1)) return true;
            if (b0 && mtYov(t.py[i - 4], qy0, qy1)) return true;
            if (a3 && mtYov(t.py[i - 5], qy0, qy1)) return true;
            if (a2 && mtYov(t.py[i - 6], qy0, qy1)) return true;
            if (a1 && mtYov(t.py[i - 7], qy0, qy1)) return true;
            if (a0 && mtYov(t.py[i - 8], qy0, qy1)) return true;
        }
    }
    return false;
#endif
    while (i & 3) { --i; if (mtLe4(t.pk[i], Q) && mtYov(t.py[i], qy0, qy1)) return true; }
    for (; i > 0; i -= 4) {
#if defined(__CUDA_ARCH__)
        const uint4 w = *reinterpret_cast<const uint4*>(&t.pk[i - 4]);
        const uint32_t w0 = w.x, w1 = w.y, w2 = w.z, w3 = w.w;
#else
        const uint32_t w0 = t.pk[i - 4], w1 = t.pk[i - 3], w2 = t.pk[i - 2], w3 = t.pk[i - 1];
#endif
        const bool a3 = mtLe4(w3, Q), a2 = mtLe4(w2, Q), a1 = mtLe4(w1, Q), a0 = mtLe4(w0, Q);
        if (a3 | a2 | a1 | a0) {
            if (a3 && mtYov(t.py[i - 1], qy0, qy1)) return true;
            if (a2 && mtYov(t.py[i - 2], qy0, qy1)) return true;
            if (a1 && mtYov(t.py[i - 3], qy0, qy1)) return true;
            if (a0 && mtYov(t.py[i - 4], qy0, qy1)) return true;
        }
    }
    return false;
}
#else
MT_HD inline bool mtOverlapsImpl(MtTree& t, int x0, int y0, int z0, int x1, int y1, int z1) {
    const int qx0 = x0 - t.sx, qz0 = z0 - t.sz, qx1 = x1 - t.sx, qz1 = z1 - t.sz;
    const int qy0 = y0 - 50, qy1 = y1 - 50;
    if (!(mtFits8(qx0) & mtFits8(qz0) & mtFits8(qx1) & mtFits8(qz1) & mtFits8(qy0) & mtFits8(qy1))) {
        t.rangeBad = 1;
        return false;
    }
    const int n = t.n;
    int i = 0;
#if MT_OVL_UNROLL > 1
    for (; i + MT_OVL_UNROLL <= n; i += MT_OVL_UNROLL) {
        uint32_t w[MT_OVL_UNROLL];
#ifdef __CUDACC__
#pragma unroll
#endif
        for (int u = 0; u < MT_OVL_UNROLL; u++) w[u] = t.pk[i + u];
#ifdef __CUDACC__
#pragma unroll
#endif
        for (int u = 0; u < MT_OVL_UNROLL; u++) {
            if ((int)(int8_t)(w[u] >> 16) >= qx0 && (int)(int8_t)w[u] <= qx1 &&
                (int)(int8_t)(w[u] >> 24) >= qz0 && (int)(int8_t)(w[u] >> 8) <= qz1) {
                const uint32_t v = t.py[i + u];
                if ((int)(int8_t)(v >> 8) >= qy0 && (int)(int8_t)v <= qy1) return true;
            }
        }
    }
#endif
    for (; i < n; i++) {
        const uint32_t w = t.pk[i];
        if ((int)(int8_t)(w >> 16) >= qx0 && (int)(int8_t)w <= qx1 &&
            (int)(int8_t)(w >> 24) >= qz0 && (int)(int8_t)(w >> 8) <= qz1) {
            const uint32_t v = t.py[i];
            if ((int)(int8_t)(v >> 8) >= qy0 && (int)(int8_t)v <= qy1) return true;
        }
    }
    return false;
}
#endif

#ifdef MT_DIAG_PROFILE
MT_HD inline bool mtOverlaps(MtTree& t, int x0, int y0, int z0, int x1, int y1, int z1) {
#ifndef __CUDA_ARCH__
    return mtOverlapsImpl(t, x0, y0, z0, x1, y1, z1);
#else
    const long long c0 = clock64(); const bool r = mtOverlapsImpl(t, x0, y0, z0, x1, y1, z1); t.ovlCyc += clock64() - c0; t.ovlCalls++; t.ovlScan += t.n; return r;
#endif
}
#else
MT_HD inline bool mtOverlaps(MtTree& t, int x0, int y0, int z0, int x1, int y1, int z1) { return mtOverlapsImpl(t, x0, y0, z0, x1, y1, z1); }
#endif

MT_HD inline int mtAbs(int v) { return v < 0 ? -v : v; }

MT_HD void mtTry(MtTree& t, int i, int j, int k, int dir, int depth);

MT_HD inline void mtPlaceStairs(MtTree& t, MtPiece s) {
    switch (s.facing) {
    default:
    case MT_N: mtTry(t, s.x0, s.y0, s.z0 - 1, MT_N, s.depth); break;
    case MT_S: mtTry(t, s.x0, s.y0, s.z1 + 1, MT_S, s.depth); break;
    case MT_W: mtTry(t, s.x0 - 1, s.y0, s.z0, MT_W, s.depth); break;
    case MT_E: mtTry(t, s.x1 + 1, s.y0, s.z0, MT_E, s.depth); break;
    }
}

MT_HD inline void mtPlaceCrossing(MtTree& t, MtPiece s) {
    const int d = s.depth;
    switch (s.facing) {
    default:
    case MT_N:
        mtTry(t, s.x0 + 1, s.y0, s.z0 - 1, MT_N, d);
        mtTry(t, s.x0 - 1, s.y0, s.z0 + 1, MT_W, d);
        mtTry(t, s.x1 + 1, s.y0, s.z0 + 1, MT_E, d);
        break;
    case MT_S:
        mtTry(t, s.x0 + 1, s.y0, s.z1 + 1, MT_S, d);
        mtTry(t, s.x0 - 1, s.y0, s.z0 + 1, MT_W, d);
        mtTry(t, s.x1 + 1, s.y0, s.z0 + 1, MT_E, d);
        break;
    case MT_W:
        mtTry(t, s.x0 + 1, s.y0, s.z0 - 1, MT_N, d);
        mtTry(t, s.x0 + 1, s.y0, s.z1 + 1, MT_S, d);
        mtTry(t, s.x0 - 1, s.y0, s.z0 + 1, MT_W, d);
        break;
    case MT_E:
        mtTry(t, s.x0 + 1, s.y0, s.z0 - 1, MT_N, d);
        mtTry(t, s.x0 + 1, s.y0, s.z1 + 1, MT_S, d);
        mtTry(t, s.x1 + 1, s.y0, s.z0 + 1, MT_E, d);
    }
    if (s.y1 - s.y0 + 1 > 3) {
        if (mtNextBool(t)) mtTry(t, s.x0 + 1, s.y0 + 3 + 1, s.z0 - 1, MT_N, d);
        if (mtNextBool(t)) mtTry(t, s.x0 - 1, s.y0 + 3 + 1, s.z0 + 1, MT_W, d);
        if (mtNextBool(t)) mtTry(t, s.x1 + 1, s.y0 + 3 + 1, s.z0 + 1, MT_E, d);
        if (mtNextBool(t)) mtTry(t, s.x0 + 1, s.y0 + 3 + 1, s.z1 + 1, MT_S, d);
    }
}

MT_HD inline void mtPlaceCorridor(MtTree& t, MtPiece s) {
    const int d = s.depth;
    const int j = mtNextInt(t, 4);

    switch (s.facing) {
    default:
    case MT_N:
        if (j <= 1)      { int y = s.y0 - 1 + mtNextInt(t, 3); mtTry(t, s.x0, y, s.z0 - 1, s.facing, d); }
        else if (j == 2) { int y = s.y0 - 1 + mtNextInt(t, 3); mtTry(t, s.x0 - 1, y, s.z0, MT_W, d); }
        else             { int y = s.y0 - 1 + mtNextInt(t, 3); mtTry(t, s.x1 + 1, y, s.z0, MT_E, d); }
        break;
    case MT_S:
        if (j <= 1)      { int y = s.y0 - 1 + mtNextInt(t, 3); mtTry(t, s.x0, y, s.z1 + 1, s.facing, d); }
        else if (j == 2) { int y = s.y0 - 1 + mtNextInt(t, 3); mtTry(t, s.x0 - 1, y, s.z1 - 3, MT_W, d); }
        else             { int y = s.y0 - 1 + mtNextInt(t, 3); mtTry(t, s.x1 + 1, y, s.z1 - 3, MT_E, d); }
        break;
    case MT_W:
        if (j <= 1)      { int y = s.y0 - 1 + mtNextInt(t, 3); mtTry(t, s.x0 - 1, y, s.z0, s.facing, d); }
        else if (j == 2) { int y = s.y0 - 1 + mtNextInt(t, 3); mtTry(t, s.x0, y, s.z0 - 1, MT_N, d); }
        else             { int y = s.y0 - 1 + mtNextInt(t, 3); mtTry(t, s.x0, y, s.z1 + 1, MT_S, d); }
        break;
    case MT_E:
        if (j <= 1)      { int y = s.y0 - 1 + mtNextInt(t, 3); mtTry(t, s.x1 + 1, y, s.z0, s.facing, d); }
        else if (j == 2) { int y = s.y0 - 1 + mtNextInt(t, 3); mtTry(t, s.x1 - 3, y, s.z0 - 1, MT_N, d); }
        else             { int y = s.y0 - 1 + mtNextInt(t, 3); mtTry(t, s.x1 - 3, y, s.z1 + 1, MT_S, d); }
    }
    if (t.aborted) return;
    if (d < 8) {
        if (s.facing != MT_N && s.facing != MT_S) {
            for (int k = s.x0 + 3; k + 3 <= s.x1; k += 5) {
                int l = mtNextInt(t, 5);
                if (l == 0)      mtTry(t, k, s.y0, s.z0 - 1, MT_N, d + 1);
                else if (l == 1) mtTry(t, k, s.y0, s.z1 + 1, MT_S, d + 1);
                if (t.aborted) return;
            }
        } else {
            for (int k = s.z0 + 3; k + 3 <= s.z1; k += 5) {
                int l = mtNextInt(t, 5);
                if (l == 0)      mtTry(t, s.x0 - 1, s.y0, k, MT_W, d + 1);
                else if (l == 1) mtTry(t, s.x1 + 1, s.y0, k, MT_E, d + 1);
                if (t.aborted) return;
            }
        }
    }
}

MT_HD inline void mtPlaceRoom(MtTree& t, MtPiece s) {
    const int xs = s.x1 - s.x0 + 1, zs = s.z1 - s.z0 + 1;
    int j = (s.y1 - s.y0 + 1) - 3 - 1;
    if (j <= 0) j = 1;
    int k;
    for (k = 0; k < xs; k += 4) {
        k += mtNextInt(t, xs);
        if (k + 3 > xs) break;
        int y = s.y0 + mtNextInt(t, j) + 1;
        mtTry(t, s.x0 + k, y, s.z0 - 1, MT_N, s.depth);
        if (t.aborted) return;
    }
    for (k = 0; k < xs; k += 4) {
        k += mtNextInt(t, xs);
        if (k + 3 > xs) break;
        int y = s.y0 + mtNextInt(t, j) + 1;
        mtTry(t, s.x0 + k, y, s.z1 + 1, MT_S, s.depth);
        if (t.aborted) return;
    }
    for (k = 0; k < zs; k += 4) {
        k += mtNextInt(t, zs);
        if (k + 3 > zs) break;
        int y = s.y0 + mtNextInt(t, j) + 1;
        mtTry(t, s.x0 - 1, y, s.z0 + k, MT_W, s.depth);
        if (t.aborted) return;
    }
    for (k = 0; k < zs; k += 4) {
        k += mtNextInt(t, zs);
        if (k + 3 > zs) break;
        int y = s.y0 + mtNextInt(t, j) + 1;
        mtTry(t, s.x1 + 1, y, s.z0 + k, MT_E, s.depth);
        if (t.aborted) return;
    }
}

MT_HD void mtTry(MtTree& t, int i, int j, int k, int dir, int l) {
    if (t.aborted) return;
    if (!(l <= 8 && mtAbs(i - t.sx) <= 80 && mtAbs(k - t.sz) <= 80)) return;

    MtPiece np; np.rails = 0;
    np.depth = (int8_t)(l + 1);
    np.facing = (int8_t)dir;
    const int m = mtNextInt(t, 100);
    if (m >= 80) {
        np.type = MT_CROSSING;
        int x0 = i, y0 = j, z0 = k, x1 = i, y1 = j + 3 - 1, z1 = k;
        if (mtNextInt(t, 4) == 0) y1 += 4;
        switch (dir) {
        default:
        case MT_N: x0 = i - 1; x1 = i + 3; z0 = k - 4; break;
        case MT_S: x0 = i - 1; x1 = i + 3; z1 = k + 3 + 1; break;
        case MT_W: x0 = i - 4; z0 = k - 1; z1 = k + 3; break;
        case MT_E: x1 = i + 3 + 1; z0 = k - 1; z1 = k + 3; break;
        }
        if (mtOverlaps(t, x0, y0, z0, x1, y1, z1)) return;
        np.x0 = x0; np.y0 = y0; np.z0 = z0; np.x1 = x1; np.y1 = y1; np.z1 = z1;
    } else if (m >= 70) {
        np.type = MT_STAIRS;
        int x0 = i, y0 = j - 5, z0 = k, x1 = i, y1 = j + 3 - 1, z1 = k;
        switch (dir) {
        default:
        case MT_N: x1 = i + 3 - 1; z0 = k - 8; break;
        case MT_S: x1 = i + 3 - 1; z1 = k + 8; break;
        case MT_W: x0 = i - 8; z1 = k + 3 - 1; break;
        case MT_E: x1 = i + 8; z1 = k + 3 - 1; break;
        }
        if (mtOverlaps(t, x0, y0, z0, x1, y1, z1)) return;
        np.x0 = x0; np.y0 = y0; np.z0 = z0; np.x1 = x1; np.y1 = y1; np.z1 = z1;
    } else {
        np.type = MT_CORRIDOR;
        int x0 = i, y0 = j, z0 = k, x1 = i, y1 = j + 3 - 1, z1 = k;
        int l2;
        for (l2 = mtNextInt(t, 3) + 2; l2 > 0; --l2) {
            const int mm = l2 * 5;
            switch (dir) {
            default:
            case MT_N: x1 = i + 3 - 1; z0 = k - (mm - 1); break;
            case MT_S: x1 = i + 3 - 1; z1 = k + mm - 1; break;
            case MT_W: x0 = i - (mm - 1); z1 = k + 3 - 1; break;
            case MT_E: x1 = i + mm - 1; z1 = k + 3 - 1; break;
            }
            if (!mtOverlaps(t, x0, y0, z0, x1, y1, z1)) break;
        }
        if (l2 <= 0) return;
        np.x0 = x0; np.y0 = y0; np.z0 = z0; np.x1 = x1; np.y1 = y1; np.z1 = z1;

        const bool hasRails = mtNextInt(t, 3) == 0;
        np.rails = hasRails;
        if (!hasRails) (void)mtNextInt(t, 23);
    }

    if (t.n >= t.budget) { t.deferred = 1; t.aborted = 1; return; }
    if (t.n >= MT_MAXP) { t.overflow = 1; t.aborted = 1; return; }
    mtPush(t, np);
    if (np.y0 < t.abortBelow) { t.aborted = 1; return; }

    switch (np.type) {
    case MT_STAIRS:   mtPlaceStairs(t, np); break;
    case MT_CROSSING: mtPlaceCrossing(t, np); break;
    default:          mtPlaceCorridor(t, np); break;
    }
}

struct MtResult {
    int status;
    int pieces, spanY, boxMinY, boxMaxY, roomFloor;
};

MT_HD inline void mtSeed(MtTree& t, uint64_t s) { t.rs = (s ^ MT_MULT) & MT_MASK; t.minRoomTop = (int)0x80000000; }

MT_HD inline void mtGenerate(MtTree& t, int cx, int cz, int abortBelow, MtResult* out,
                          int budget = 0x7fffffff) {
    t.n = 0; t.aborted = 0; t.overflow = 0; t.deferred = 0; t.abortBelow = abortBelow; t.budget = budget;
    t.rangeBad = 0; t.minY = 0x7fffffff; t.maxY = -0x7fffffff - 1;

    (void)mtNext(t, 26);
    (void)mtNext(t, 27);

    MtPiece room;
    room.type = MT_ROOM; room.facing = -1; room.depth = 0;
    room.x0 = cx * 16 + 2;
    room.y0 = 50;
    room.z0 = cz * 16 + 2;
    room.x1 = room.x0 + 7 + mtNextInt(t, 6);
    room.y1 = 54 + mtNextInt(t, 6);
    room.z1 = room.z0 + 7 + mtNextInt(t, 6);
    t.sx = room.x0; t.sz = room.z0;
    mtPush(t, room);

    mtPlaceRoom(t, room);

    if (t.deferred) { out->status = 3; return; }
    if (t.overflow || t.rangeBad) { out->status = 2; return; }
    if (t.aborted)  { out->status = 1; return; }

    const int minY = t.minY, maxY = t.maxY;
    const int span = maxY - minY + 1;
    int l = span + (-64) + 1;
    if (l < 53) l += mtNextInt(t, 53 - l);
    const int m = l - maxY;
    out->status = 0;
    out->pieces = t.n;
    out->spanY = span;
    out->boxMinY = minY + m;
    out->boxMaxY = maxY + m;
    out->roomFloor = 50 + m;
}
