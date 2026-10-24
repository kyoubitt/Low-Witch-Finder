#pragma once
#include "solver_kernels.cuh"
#include "taskspace.h"
#ifndef FUSED_ITEM_UNROLL
#define FUSED_ITEM_UNROLL 1
#endif
#define FUSED_PRAGMA_STR2(x) _Pragma(#x)
#define FUSED_PRAGMA_STR(x) FUSED_PRAGMA_STR2(x)
#define FUSED_NLID 192
struct FusedRegion { int rx, rz; unsigned long long Creg; };

__device__ unsigned char g_nibTab[8 * 256];
static void fusedNibTableUpload() {
    unsigned char tab[8 * 256];
    for (int r = 0; r < 8; r++) for (int q = 0; q < 16; q++) for (int y = 0; y < 16; y++) {
        int xn = 0; const int rho16 = 2 * r;
        for (int i = 0; i < 4; i++) { const int v = (q + ((rho16 * xn) & 15)) & 15; if (((y ^ v) >> i) & 1) xn |= 1 << i; }
        tab[r * 256 + q * 16 + y] = (unsigned char)xn;
    }
    cudaMemcpyToSymbol(g_nibTab, tab, sizeof tab);
}

static inline int fusedValuation(int v) { if (v == 0) return 99; int k = 0; while (!(v & 1)) { v >>= 1; k++; } return k; }
static int fusedOffsetList(int tier, unsigned char* ox, unsigned char* oz) {
    int n = 0;
    for (int a = 0; a < 24; a++) for (int b = 0; b < 24; b++) {
        const int va = fusedValuation(a), vb = fusedValuation(b), m = va < vb ? va : vb;
        const bool t1 = (va != vb) && m == 0, t2 = (va != vb) && m >= 1, t3 = (va == vb) && va < 99;
        if ((tier == 1 && t1) || (tier == 2 && t2) || (tier == 3 && t3)) { ox[n] = (unsigned char)a; oz[n] = (unsigned char)b; n++; }
    }
    return n;
}
__device__ __forceinline__ unsigned inv32d(unsigned a) { unsigned x = a; x *= 2u - a * x; x *= 2u - a * x; x *= 2u - a * x; x *= 2u - a * x; x *= 2u - a * x; return x; }
__device__ __forceinline__ int valD(int c) { return c == 0 ? 40 : (__ffs(c) - 1); }
__device__ __forceinline__ int fusedListIdx(int m, int cls, int cxr, int czr) { return m * 192 + cls * 64 + ((cxr >> 1) & 7) * 8 + ((czr >> 1) & 7); }

__global__ void __launch_bounds__(256) fusedBuildKernel(const unsigned* __restrict__ c20s, unsigned long long X, int mLo, int mHi, unsigned* __restrict__ pool, unsigned long long poolCap,
                                                        unsigned long long* __restrict__ poolCnt, unsigned long long* __restrict__ starts, unsigned* __restrict__ counts) {
    __shared__ unsigned sn; __shared__ unsigned long long sbase;
    const unsigned nLid = 192u * (unsigned)(mHi - mLo + 1);
    const unsigned gs = blockIdx.x / nLid, l = blockIdx.x - gs * nLid;
    const int m = mLo + (int)(l / 192u); const unsigned r = l % 192u;
    const unsigned cls = r >> 6; const unsigned a = (r & 63) >> 3, b = r & 7;
    const unsigned cxr = cls != 1 ? ((a << 1) | 1u) : (a << 1), czr = cls == 0 ? (b << 1) : ((b << 1) | 1u);
    const unsigned key = (((cxr << m) & 7u) << 3) | ((czr << m) & 7u);
    const unsigned lo = c_hutQoff[key], hi = c_hutQoff[key + 1];
    const unsigned creg20 = c20s[gs], sa32 = (unsigned)SA, sa20 = sa32 & 0xFFFFFu, X32 = (unsigned)X, pinMask = (1u << m) - 1u;
    if (threadIdx.x == 0) sn = 0;
    __syncthreads();
    for (int pass = 0; pass < 2; pass++) {
        for (unsigned i = lo + threadIdx.x; i < hi; i += blockDim.x) {
            const unsigned q = g_hutQ[i];
            const unsigned t = ((q - creg20) & 0xFFFFFu) ^ sa20;
            const unsigned S0 = t ^ sa32;
            if (((S0 ^ X32) & pinMask) != 0u) continue;
            const unsigned h2 = (SA2LO * t + SK2LO) >> 16, h4 = (SA4LO * t + SK4LO) >> 16;
            if ((((S0 >> m) ^ (cxr * h2) ^ (czr * h4) ^ (X32 >> m)) & 15u) == 0u) {
                if (pass == 0) atomicAdd(&sn, 1u); else { const unsigned s = atomicAdd(&sn, 1u); pool[sbase + s] = t; }
            }
        }
        __syncthreads();
        if (pass == 0) {
            const unsigned n = sn;
            if (threadIdx.x == 0) { sbase = atomicAdd(poolCnt, (unsigned long long)n); starts[gs * nLid + l] = sbase; counts[gs * nLid + l] = n; sn = 0; }
            __syncthreads();
            if (sbase + (unsigned long long)n > poolCap) return;
        }
    }
}

template <int FBLK>
__global__ void __launch_bounds__(FBLK) fusedChainKernel(const FusedRegion* __restrict__ regions, unsigned nReg, const unsigned char* __restrict__ jOx, const unsigned char* __restrict__ jOz, unsigned nJ, int mLo,
                                                         const unsigned* __restrict__ pool, const unsigned long long* __restrict__ starts, const unsigned* __restrict__ counts,
                                                         unsigned long long X, SolveHit* __restrict__ out, unsigned long long* __restrict__ outCnt, unsigned long long cap) {
    __shared__ unsigned char tab[8 * 256];
    for (int i = threadIdx.x; i < 8 * 256 / 4; i += FBLK) ((unsigned*)tab)[i] = ((const unsigned*)g_nibTab)[i];
    __syncthreads();
    const unsigned total = nReg * nJ;
    const unsigned X32 = (unsigned)X;
    for (unsigned w = blockIdx.x; w < total; w += gridDim.x) {
        const unsigned j = w / nReg, ri = w - j * nReg;
        const FusedRegion R = regions[ri];
        const int ox = jOx[j], oz = jOz[j];
        const int cx = 32 * R.rx + ox, cz = 32 * R.rz + oz;
        const int vx = valD(cx), vz = valD(cz);
        const bool oddX = vx < vz; const int m = oddX ? vx : vz;
        const int cxr = (cx >> m) & 15, czr = (cz >> m) & 15;
        const unsigned lid = (unsigned)fusedListIdx(m, oddX ? 0 : 1, cxr, czr) - 192u * (unsigned)mLo;
        const unsigned n = counts[lid];
        const unsigned* __restrict__ list = pool + starts[lid];
        const unsigned cx32 = (unsigned)cx, cz32 = (unsigned)cz;
        const unsigned cxA = cx32 * SA2LO, czA = cz32 * SA4LO;
        const unsigned coefO = (oddX ? cxA : czA) >> m, coefE = (oddX ? czA : cxA) >> m;
        const unsigned inv = inv32d(coefO), rho = coefE * inv;
        const unsigned char* row = tab + ((rho & 15u) >> 1) * 256;
        const unsigned uMask = (1u << (28 - m)) - 1u;
FUSED_PRAGMA_STR(unroll FUSED_ITEM_UNROLL)
        for (unsigned i = threadIdx.x; i < n; i += FBLK) {
            const unsigned long long t20 = list[i];
            const unsigned hx = (unsigned)((SA2 * t20 + SK2) >> 16), hz = (unsigned)((SA4 * t20 + SK4) >> 16);
            const unsigned PoF = oddX ? cx32 * hx : cz32 * hz, PeF = oddX ? cz32 * hz : cx32 * hx;
            const unsigned Po = PoF >> m, Pe = PeF >> m;
            const unsigned S0 = (unsigned)(t20 ^ SA);
            const unsigned Kq = Pe - rho * Po;
            unsigned x = Po & 15u, Aq = Kq + rho * x, V = 0;
#pragma unroll
            for (int kk = 4; kk < 32; kk += 4) {
                const unsigned Y = ((S0 ^ (V << 16)) ^ X32) >> m;
                const unsigned xn = row[(((Aq >> kk) & 15u) << 4) + ((Y >> kk) & 15u)];
                x |= xn << kk; Aq += rho * (xn << kk);
                V = (x - Po) * inv;
            }
            const unsigned u0 = (V >> 4) & uMask;
            for (unsigned tl = 0; tl < (1u << m); tl++) {
                const unsigned u = u0 | (tl << (28 - m));
                const unsigned long long t = t20 | ((unsigned long long)u << 20); const unsigned t32 = (unsigned)t;
                const unsigned h2 = hx + ((SA2LO << 4) * u), h4 = hz + ((SA4LO << 4) * u);
                const unsigned A1 = (unsigned)SA, A3 = (unsigned)((SA2 * SA) & S48_MASK), K3 = (unsigned)((SC * (SA2 + SA + 1)) & S48_MASK);
                const unsigned h1lo = ((A1 * t32 + (unsigned)SC) >> 16) & 0xFFFFu, h3lo = ((A3 * t32 + K3) >> 16) & 0xFFFFu;
                const unsigned long long a = ((unsigned long long)((h1lo - (h2 >> 31)) & 0xFFFFu) << 32) + h2, b = ((unsigned long long)((h3lo - (h4 >> 31)) & 0xFFFFu) << 32) + h4;
                const unsigned long long S = (t ^ SA) & S48_MASK;
                if ((((unsigned long long)((long long)cx * (long long)a) ^ (unsigned long long)((long long)cz * (long long)b) ^ S ^ X) & S48_MASK) == 0ULL && hutExact(S, R.Creg, cx & 31, cz & 31)) {
                    const unsigned long long slot = atomicAdd(outCnt, 1ULL);
                    if (slot < cap) { out[slot].S = S; out[slot].X = X; out[slot].cx = cx; out[slot].cz = cz; out[slot].val = 0; out[slot].pad = 0; }
                }
            }
        }
    }
}

__device__ unsigned short g_nibMask[8 * 256];
__device__ unsigned short g_nibMaskV[3 * 8 * 256];
static void fusedMaskTableUpload() {
    static unsigned short tab[8 * 256], tabv[3 * 8 * 256];
    for (int r = 0; r < 8; r++) for (int q = 0; q < 16; q++) for (int y = 0; y < 16; y++) {
        const int rho16 = 2 * r + 1; unsigned short m = 0;
        for (int xn = 0; xn < 16; xn++) if ((xn ^ ((q + ((rho16 * xn) & 15)) & 15)) == y) m |= (unsigned short)(1u << xn);
        tab[r * 256 + q * 16 + y] = m;
        for (int vb = 3; vb >= 1; vb--) { unsigned short mv = 0; for (int xn = 0; xn < (1 << vb); xn++) if ((((xn ^ ((q + ((rho16 * xn) & 15)) & 15)) ^ y) & ((1 << vb) - 1)) == 0) mv |= (unsigned short)(1u << xn); tabv[(3 - vb) * 2048 + r * 256 + q * 16 + y] = mv; }
    }
    cudaMemcpyToSymbol(g_nibMask, tab, sizeof tab); cudaMemcpyToSymbol(g_nibMaskV, tabv, sizeof tabv);
}
#define EQ_LEAF(XF) { \
    const unsigned V_ = ((XF) - Po) * inv; const unsigned u0 = (V_ >> 4) & uMask; \
    for (unsigned tl = 0; tl < (1u << m); tl++) { \
        const unsigned u = u0 | (tl << (28 - m)); \
        const unsigned long long t = t20 | ((unsigned long long)u << 20); const unsigned t32 = (unsigned)t; \
        const unsigned h2 = hx + ((SA2LO << 4) * u), h4 = hz + ((SA4LO << 4) * u); \
        const unsigned A1 = (unsigned)SA, A3 = (unsigned)((SA2 * SA) & S48_MASK), K3 = (unsigned)((SC * (SA2 + SA + 1)) & S48_MASK); \
        const unsigned h1lo = ((A1 * t32 + (unsigned)SC) >> 16) & 0xFFFFu, h3lo = ((A3 * t32 + K3) >> 16) & 0xFFFFu; \
        const unsigned long long a = ((unsigned long long)((h1lo - (h2 >> 31)) & 0xFFFFu) << 32) + h2, b = ((unsigned long long)((h3lo - (h4 >> 31)) & 0xFFFFu) << 32) + h4; \
        const unsigned long long S = (t ^ SA) & S48_MASK; \
        if ((((unsigned long long)((long long)cx * (long long)a) ^ (unsigned long long)((long long)cz * (long long)b) ^ S ^ X) & S48_MASK) == 0ULL && hutExact(S, R.Creg, cx & 31, cz & 31)) { \
            const unsigned long long slot = atomicAdd(outCnt, 1ULL); \
            if (slot < cap) { out[slot].S = S; out[slot].X = X; out[slot].cx = cx; out[slot].cz = cz; out[slot].val = 0; out[slot].pad = 0; } } } }

#define EQ_LVL(XP, KK, XN, ...) { \
    const unsigned Aq_ = Kq + rho * (XP), V_ = ((XP) - Po) * inv, Y_ = ((S0 ^ (V_ << 16)) ^ X32) >> m; \
    unsigned msk_ = row[(((Aq_ >> (KK)) & 15u) << 4) + ((Y_ >> (KK)) & 15u)]; \
    while (msk_) { const unsigned xn_ = __ffs(msk_) - 1; msk_ &= msk_ - 1; const unsigned XN = (XP) | (xn_ << (KK)); __VA_ARGS__ } }
template <int FBLK>
__global__ void __launch_bounds__(FBLK) fusedChainEqKernel(const FusedRegion* __restrict__ regions, unsigned nReg, const unsigned char* __restrict__ jOx, const unsigned char* __restrict__ jOz, unsigned nJ, int mLo,
                                                           const unsigned* __restrict__ pool, const unsigned long long* __restrict__ starts, const unsigned* __restrict__ counts,
                                                           unsigned long long X, SolveHit* __restrict__ out, unsigned long long* __restrict__ outCnt, unsigned long long cap) {
    __shared__ unsigned tabw[8 * 256 / 2];
    for (int i = threadIdx.x; i < 8 * 256 / 2; i += FBLK) tabw[i] = ((const unsigned*)g_nibMask)[i];
    __syncthreads();
    const unsigned short* tabm = (const unsigned short*)tabw;
    const unsigned total = nReg * nJ;
    const unsigned X32 = (unsigned)X;
    for (unsigned w = blockIdx.x; w < total; w += gridDim.x) {
        const unsigned j = w / nReg, ri = w - j * nReg;
        const FusedRegion R = regions[ri];
        const int ox = jOx[j], oz = jOz[j];
        const int cx = 32 * R.rx + ox, cz = 32 * R.rz + oz;
        const int m = valD(cx);
        const int cxr = (cx >> m) & 15, czr = (cz >> m) & 15;
        const unsigned lid = (unsigned)fusedListIdx(m, 2, cxr, czr) - 192u * (unsigned)mLo;
        const unsigned n = counts[lid];
        const unsigned* __restrict__ list = pool + starts[lid];
        const unsigned cx32 = (unsigned)cx, cz32 = (unsigned)cz;
        const unsigned cxA = cx32 * SA2LO, czA = cz32 * SA4LO;
        const unsigned coefO = cxA >> m, coefE = czA >> m;
        const unsigned inv = inv32d(coefO), rho = coefE * inv;
        const unsigned short* row = tabm + ((rho & 15u) >> 1) * 256;
        const unsigned short* rowV = g_nibMaskV + (m >= 1 && m <= 3 ? (3 - (4 - m)) * 2048 : 0) + ((rho & 15u) >> 1) * 256;
        const unsigned uMask = (1u << (28 - m)) - 1u;
        for (unsigned i = threadIdx.x; i < n; i += FBLK) {
            const unsigned long long t20 = list[i];
            const unsigned hx = (unsigned)((SA2 * t20 + SK2) >> 16), hz = (unsigned)((SA4 * t20 + SK4) >> 16);
            const unsigned Po = (cx32 * hx) >> m, Pe = (cz32 * hz) >> m;
            const unsigned S0 = (unsigned)(t20 ^ SA);
            const unsigned Kq = Pe - rho * Po;
            const unsigned x0 = Po & 15u;
            EQ_LVL(x0, 4, x1,
              EQ_LVL(x1, 8, x2,
                EQ_LVL(x2, 12, x3,
                  EQ_LVL(x3, 16, x4,
                    EQ_LVL(x4, 20, x5,
                      EQ_LVL(x5, 24, x6,
                        if (m == 0) {
                            EQ_LVL(x6, 28, x7, EQ_LEAF(x7))
                        } else if (m < 4) {
                            const unsigned Aq_ = Kq + rho * x6, V_ = (x6 - Po) * inv, Y_ = ((S0 ^ (V_ << 16)) ^ X32) >> m;
                            unsigned msk_ = rowV[(((Aq_ >> 28) & 15u) << 4) + ((Y_ >> 28) & 15u)];
                            while (msk_) { const unsigned xn_ = __ffs(msk_) - 1; msk_ &= msk_ - 1; const unsigned x7 = x6 | (xn_ << 28); EQ_LEAF(x7) }
                        } else {
                            EQ_LEAF(x6)
                        }
                      )))))
            )
        }
    }
}
