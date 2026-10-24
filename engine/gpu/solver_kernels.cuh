#pragma once
#include <cstdint>
#include <cstdlib>
#ifndef S48_MASK
#define S48_MASK 0xFFFFFFFFFFFFULL
#endif

#define SA 0x5DEECE66DULL
#define SC 0xBULL
#define SA2 ((SA * SA) & S48_MASK)
#define SK2 ((SC * (SA + 1)) & S48_MASK)
#define SA4 ((SA2 * SA2) & S48_MASK)
#define SK4 ((SC * (SA * SA * SA + SA * SA + SA + 1)) & S48_MASK)
struct SolveTask { unsigned long long X; unsigned long long Creg; int cx, cz; unsigned val; unsigned pad; };
struct SolveHit  { unsigned long long S, X; int cx, cz; unsigned val; unsigned pad; };

__device__ __forceinline__ unsigned eqLow(unsigned long long t, int nb, unsigned X32, unsigned cx32, unsigned cz32) {
    const unsigned h2 = (unsigned)(((SA2 * t + SK2)) >> 16), h4 = (unsigned)(((SA4 * t + SK4)) >> 16);
    const unsigned e = (unsigned)(t ^ SA) ^ (cx32 * h2) ^ (cz32 * h4) ^ X32;
    return nb >= 32 ? e : (e & ((1u << nb) - 1u));
}

__device__ __forceinline__ bool hutPrune20(unsigned long long t, unsigned creg20, int ox, int oz) {
    const unsigned rs = (((unsigned)(t ^ SA)) + creg20) & 0xFFFFFu;
    const unsigned r0 = rs ^ ((unsigned)SA & 0xFFFFFu);
    const unsigned r1 = (r0 * (unsigned)SA + 0xBu) & 0xFFFFFu;
    if (((r1 >> 17) & 7u) != (unsigned)(ox & 7)) return false;
    const unsigned r2 = (r1 * (unsigned)SA + 0xBu) & 0xFFFFFu;
    return ((r2 >> 17) & 7u) == (unsigned)(oz & 7);
}
__device__ __forceinline__ void appendWarp(bool pass, unsigned long long item, unsigned long long* __restrict__ list, unsigned long long* __restrict__ cnt, unsigned long long cap) {
    const unsigned m = __ballot_sync(0xFFFFFFFFu, pass);
    if (!m) return;
    const int lane = threadIdx.x & 31;
    unsigned long long base = 0;
    if (lane == __ffs(m) - 1) base = atomicAdd(cnt, (unsigned long long)__popc(m));
    base = __shfl_sync(0xFFFFFFFFu, base, __ffs(m) - 1);
    if (pass) { const unsigned long long slot = base + __popc(m & ((1u << lane) - 1)); if (slot < cap) list[slot] = item; }
}
__global__ void solveEarlyKernel(const SolveTask* __restrict__ tasks, unsigned long long nThreads, unsigned long long* __restrict__ list, unsigned long long* __restrict__ cnt, unsigned long long cap) {
    const unsigned long long tid = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (tid < nThreads) {
        const unsigned task = (unsigned)(tid >> 16);
        const SolveTask T = tasks[task];
        const unsigned X32 = (unsigned)T.X, cx32 = (unsigned)T.cx, cz32 = (unsigned)T.cz;
        const unsigned long long t0 = tid & 0xFFFFULL;

        for (int b16 = 0; b16 < 2; b16++) {
            const unsigned long long t1 = t0 | ((unsigned long long)b16 << 16);
            if (eqLow(t1, 1, X32, cx32, cz32)) continue;
            for (int b17 = 0; b17 < 2; b17++) {
                const unsigned long long t2 = t1 | ((unsigned long long)b17 << 17);
                if (eqLow(t2, 2, X32, cx32, cz32)) continue;
                for (int b18 = 0; b18 < 2; b18++) {
                    const unsigned long long t3 = t2 | ((unsigned long long)b18 << 18);
                    if (eqLow(t3, 3, X32, cx32, cz32)) continue;
                    for (int b19 = 0; b19 < 2; b19++) {
                        const unsigned long long t4 = t3 | ((unsigned long long)b19 << 19);
                        if (eqLow(t4, 4, X32, cx32, cz32)) continue;
                        if (!T.pad && !hutPrune20(t4, (unsigned)T.Creg & 0xFFFFFu, T.cx & 31, T.cz & 31)) continue;
                        const unsigned long long slot = atomicAdd(cnt, 1ULL);
                        if (slot < cap) list[slot] = ((unsigned long long)task << 20) | t4;
                    }
                }
            }
        }
    }
}
__device__ __forceinline__ unsigned long long carverFull(unsigned long long S, int cx, int cz) {
    unsigned long long s = (S ^ SA) & S48_MASK;
    s = (s * SA + SC) & S48_MASK; const int h1 = (int)(s >> 16);
    s = (s * SA + SC) & S48_MASK; const int h2 = (int)(s >> 16);
    s = (s * SA + SC) & S48_MASK; const int h3 = (int)(s >> 16);
    s = (s * SA + SC) & S48_MASK; const int h4 = (int)(s >> 16);
    const long long a = (long long)(((unsigned long long)(long long)h1 << 32) + (unsigned long long)(long long)h2), b = (long long)(((unsigned long long)(long long)h3 << 32) + (unsigned long long)(long long)h4);
    return ((unsigned long long)((long long)cx * a) ^ (unsigned long long)((long long)cz * b) ^ S) & S48_MASK;
}
__device__ __forceinline__ bool hutExact(unsigned long long S, unsigned long long Creg, int ox, int oz) {
    unsigned long long s = (((S + Creg) & S48_MASK) ^ SA) & S48_MASK; int v[2];
    for (int k = 0; k < 2; k++) for (;;) { s = (s * SA + SC) & S48_MASK; const int bits = (int)(s >> 17), val = bits % 24; if (bits - val + 23 >= 0) { v[k] = val; break; } }
    return v[0] == ox && v[1] == oz;
}
__global__ void solveLateKernel(const SolveTask* __restrict__ tasks, const unsigned long long* __restrict__ items, unsigned long long n,
                                SolveHit* __restrict__ out, unsigned long long* __restrict__ outCnt, unsigned long long cap) {
    const unsigned long long k = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (k >= n) return;
    const unsigned long long it = items[k];
    const unsigned task = (unsigned)(it >> 20);
    const SolveTask T = tasks[task];
    const unsigned X32 = (unsigned)T.X, cx32 = (unsigned)T.cx, cz32 = (unsigned)T.cz;
    unsigned long long t = it & 0xFFFFFULL; int j = 20; unsigned pend = 0;
    for (;;) {
        bool descended = false;
        if (j == 48) {
            const unsigned long long S = (t ^ SA) & S48_MASK;
            if (carverFull(S, T.cx, T.cz) == T.X && (T.pad || hutExact(S, T.Creg, T.cx & 31, T.cz & 31))) {
                const unsigned long long slot = atomicAdd(outCnt, 1ULL);
                if (slot < cap) { out[slot].S = S; out[slot].X = T.X; out[slot].cx = T.cx; out[slot].cz = T.cz; out[slot].val = T.val; }
            }
        } else {
            if (!eqLow(t, j + 1 - 16, X32, cx32, cz32)) { pend |= 1u << (j - 20); j++; descended = true; }
            else {
                const unsigned long long t1 = t | (1ULL << j);
                if (!eqLow(t1, j + 1 - 16, X32, cx32, cz32)) { t = t1; j++; descended = true; }
            }
        }
        if (descended) continue;
        for (;;) {
            if (!pend) return;
            const int d = 31 - __clz(pend); pend &= ~(1u << d);
            j = d + 20; t = (t & ((1ULL << j) - 1)) | (1ULL << j);
            if (!eqLow(t, j + 1 - 16, X32, cx32, cz32)) { j++; break; }
        }
    }
}

__constant__ unsigned c_SA2s[48], c_SA4s[48];
__global__ void __launch_bounds__(256) solveEarly2Kernel(const SolveTask* __restrict__ tasks, unsigned long long* __restrict__ list, unsigned long long* __restrict__ cnt, unsigned long long cap) {
    __shared__ unsigned tab[256 * 16];
    const unsigned task = blockIdx.x;
    const SolveTask T = tasks[task];
    const unsigned cx4 = (unsigned)T.cx & 15u, cz4 = (unsigned)T.cz & 15u, X4 = (unsigned)T.X & 15u;
    const unsigned a2 = (unsigned)SA2 & 15u, a4 = (unsigned)SA4 & 15u;
    for (int i = threadIdx.x; i < 256 * 16; i += blockDim.x) tab[i] = 0;
    __syncthreads();
    {
        const int idx = threadIdx.x;
        const unsigned B2 = (unsigned)idx >> 4, B4 = (unsigned)idx & 15u;
#pragma unroll
        for (unsigned m = 0; m < 16; m++) {
            const unsigned h2 = (B2 + a2 * m) & 15u, h4 = (B4 + a4 * m) & 15u;
            const unsigned g = ((cx4 * h2) & 15u) ^ ((cz4 * h4) & 15u);
            atomicOr(&tab[idx * 16 + g], 1u << m);
        }
    }
    __syncthreads();
    const unsigned creg20 = (unsigned)T.Creg & 0xFFFFFu;
    const int ox = T.cx & 31, oz = T.cz & 31;
    for (unsigned t0 = threadIdx.x; t0 < 65536u; t0 += blockDim.x) {
        const unsigned long long b2 = SA2 * (unsigned long long)t0 + SK2, b4 = SA4 * (unsigned long long)t0 + SK4;
        const unsigned B2 = (unsigned)(b2 >> 16) & 15u, B4 = (unsigned)(b4 >> 16) & 15u;
        const unsigned v = (((unsigned)t0 ^ (unsigned)SA) ^ X4) & 15u;
        unsigned mask = tab[((B2 << 4) | B4) * 16 + v];
        while (mask) {
            const unsigned m = __ffs(mask) - 1; mask &= mask - 1;
            const unsigned long long t4 = (unsigned long long)t0 | ((unsigned long long)m << 16);
            if (!T.pad && !hutPrune20(t4, creg20, ox, oz)) continue;
            const unsigned long long slot = atomicAdd(cnt, 1ULL);
            if (slot < cap) list[slot] = ((unsigned long long)task << 20) | t4;
        }
    }
}
__global__ void solveLate2Kernel(const SolveTask* __restrict__ tasks, const unsigned long long* __restrict__ items, unsigned long long n,
                                 SolveHit* __restrict__ out, unsigned long long* __restrict__ outCnt, unsigned long long cap) {
    const unsigned long long k = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (k >= n) return;
    const unsigned long long it = items[k];
    const unsigned task = (unsigned)(it >> 20);
    const SolveTask T = tasks[task];
    const unsigned X32 = (unsigned)T.X, cx32 = (unsigned)T.cx, cz32 = (unsigned)T.cz;
    unsigned long long t = it & 0xFFFFFULL;
    unsigned P = cx32 * (unsigned)(((SA2 * t + SK2)) >> 16), Q = cz32 * (unsigned)(((SA4 * t + SK4)) >> 16), S = (unsigned)(t ^ SA);
    int j = 20; unsigned pend = 0;
    for (;;) {
        bool descended = false;
        if (j == 48) {
            const unsigned long long Sfull = (t ^ SA) & S48_MASK;
            if (carverFull(Sfull, T.cx, T.cz) == T.X && (T.pad || hutExact(Sfull, T.Creg, T.cx & 31, T.cz & 31))) {
                const unsigned long long slot = atomicAdd(outCnt, 1ULL);
                if (slot < cap) { out[slot].S = Sfull; out[slot].X = T.X; out[slot].cx = T.cx; out[slot].cz = T.cz; out[slot].val = T.val; }
            }
        } else {
            const int kb = j - 16;
            if ((((S ^ P ^ Q ^ X32) >> kb) & 1u) == 0u) { pend |= 1u << (j - 20); j++; descended = true; }
            else {
                const unsigned P1 = P + cx32 * c_SA2s[j], Q1 = Q + cz32 * c_SA4s[j], S1 = (j < 32) ? (S ^ (1u << j)) : S;
                if ((((S1 ^ P1 ^ Q1 ^ X32) >> kb) & 1u) == 0u) { t |= 1ULL << j; P = P1; Q = Q1; S = S1; j++; descended = true; }
            }
        }
        if (descended) continue;
        for (;;) {
            if (!pend) return;
            const int d = 31 - __clz(pend); pend &= ~(1u << d);
            const int jj = d + 20;

            const unsigned long long tt = t & ((1ULL << jj) - 1);

            const unsigned Pn = cx32 * (unsigned)(((SA2 * tt + SK2)) >> 16), Qn = cz32 * (unsigned)(((SA4 * tt + SK4)) >> 16), Sn = (unsigned)(tt ^ SA);
            const int kb = jj - 16;
            const unsigned P1 = Pn + cx32 * c_SA2s[jj], Q1 = Qn + cz32 * c_SA4s[jj], S1 = (jj < 32) ? (Sn ^ (1u << jj)) : Sn;
            if ((((S1 ^ P1 ^ Q1 ^ X32) >> kb) & 1u) == 0u) { t = tt | (1ULL << jj); P = P1; Q = Q1; S = S1; j = jj + 1; break; }
        }
    }
}

#define SA2LO ((unsigned)SA2)
#define SA4LO ((unsigned)SA4)
#define SK2LO ((unsigned)SK2)
#define SK4LO ((unsigned)SK4)
__global__ void __launch_bounds__(256) solveEarly3Kernel(const SolveTask* __restrict__ tasks, unsigned long long* __restrict__ listA, unsigned long long* __restrict__ cntA,
                                                         unsigned long long* __restrict__ listB, unsigned long long* __restrict__ cntB, unsigned long long capA, unsigned long long capB) {
    __shared__ unsigned tab[256 * 16];
    const unsigned task = blockIdx.x;
    const SolveTask T = tasks[task];
    const unsigned cx4 = (unsigned)T.cx & 15u, cz4 = (unsigned)T.cz & 15u, X4 = (unsigned)T.X & 15u;
    const unsigned a2 = SA2LO & 15u, a4 = SA4LO & 15u;
    for (int i = threadIdx.x; i < 256 * 16; i += blockDim.x) tab[i] = 0;
    __syncthreads();
    {
        const int idx = threadIdx.x;
        const unsigned B2 = (unsigned)idx >> 4, B4 = (unsigned)idx & 15u;
#pragma unroll
        for (unsigned m = 0; m < 16; m++) {
            const unsigned h2 = (B2 + a2 * m) & 15u, h4 = (B4 + a4 * m) & 15u;
            const unsigned g = ((cx4 * h2) & 15u) ^ ((cz4 * h4) & 15u);
            atomicOr(&tab[idx * 16 + g], 1u << m);
        }
    }
    __syncthreads();
    const unsigned creg20 = (unsigned)T.Creg & 0xFFFFFu;
    const int ox = T.cx & 31, oz = T.cz & 31;
    const bool differ = ((T.cx ^ T.cz) & 1) != 0;
    unsigned long long* __restrict__ list = differ ? listA : listB; unsigned long long* __restrict__ cnt = differ ? cntA : cntB;
    unsigned t0 = threadIdx.x;
    unsigned b2 = SA2LO * t0 + SK2LO, b4 = SA4LO * t0 + SK4LO;
    const unsigned d2 = SA2LO * blockDim.x, d4 = SA4LO * blockDim.x;
    for (; t0 < 65536u; t0 += blockDim.x, b2 += d2, b4 += d4) {
        const unsigned B2 = (b2 >> 16) & 15u, B4 = (b4 >> 16) & 15u;
        const unsigned v = ((t0 ^ (unsigned)SA) ^ X4) & 15u;
        unsigned mask = tab[((B2 << 4) | B4) * 16 + v];
        while (mask) {
            const unsigned m = __ffs(mask) - 1; mask &= mask - 1;
            const unsigned long long t4 = (unsigned long long)t0 | ((unsigned long long)m << 16);
            if (!T.pad && !hutPrune20(t4, creg20, ox, oz)) continue;
            const unsigned long long slot = atomicAdd(cnt, 1ULL);
            if (slot < (differ ? capA : capB)) list[slot] = ((unsigned long long)task << 20) | t4;
        }
    }
}

__device__ unsigned g_hutQ[1 << 20];
__constant__ unsigned c_hutQoff[65];
static void hutPruneListUpload() {
    const unsigned sa32 = (unsigned)SA, sa20 = sa32 & 0xFFFFFu;
    unsigned* key = (unsigned*)malloc((size_t)(1 << 20) * sizeof(unsigned)); unsigned* list = (unsigned*)malloc((size_t)(1 << 20) * sizeof(unsigned)); unsigned off[65] = {0}, fill[64] = {0};
    for (unsigned q = 0; q < (1u << 20); q++) {
        const unsigned r0 = q ^ sa20, r1 = (r0 * sa32 + 0xBu) & 0xFFFFFu, r2 = (r1 * sa32 + 0xBu) & 0xFFFFFu;
        key[q] = (((r1 >> 17) & 7u) << 3) | ((r2 >> 17) & 7u); off[key[q] + 1]++;
    }
    for (int k = 0; k < 64; k++) off[k + 1] += off[k];
    for (unsigned q = 0; q < (1u << 20); q++) list[off[key[q]] + fill[key[q]]++] = q;
    cudaMemcpyToSymbol(g_hutQ, list, (size_t)(1 << 20) * sizeof(unsigned)); cudaMemcpyToSymbol(c_hutQoff, off, sizeof off);
    free(key); free(list);
}
#define EARLY4_STAGE 4096
__global__ void __launch_bounds__(256) solveEarly4Kernel(const SolveTask* __restrict__ tasks, unsigned long long* __restrict__ listA, unsigned long long* __restrict__ cntA,
                                                         unsigned long long* __restrict__ listB, unsigned long long* __restrict__ cntB, unsigned long long capA, unsigned long long capB) {
    __shared__ unsigned sbuf[EARLY4_STAGE]; __shared__ unsigned sn; __shared__ unsigned long long sbase;
    const unsigned task = blockIdx.x;
    const SolveTask T = tasks[task];
    const unsigned X32 = (unsigned)T.X, cx32 = (unsigned)T.cx, cz32 = (unsigned)T.cz;
    const unsigned key = ((cx32 & 7u) << 3) | (cz32 & 7u);
    const unsigned lo = c_hutQoff[key], hi = c_hutQoff[key + 1];
    const unsigned creg20 = (unsigned)T.Creg & 0xFFFFFu, sa32 = (unsigned)SA, sa20 = sa32 & 0xFFFFFu;
    const bool differ = ((T.cx ^ T.cz) & 1) != 0;
    unsigned long long* __restrict__ list = differ ? listA : listB; unsigned long long* __restrict__ cnt = differ ? cntA : cntB; const unsigned long long cap = differ ? capA : capB;
    if (threadIdx.x == 0) sn = 0;
    __syncthreads();
    for (unsigned i = lo + threadIdx.x; i < hi; i += blockDim.x) {
        const unsigned q = g_hutQ[i];
        const unsigned t = ((q - creg20) & 0xFFFFFu) ^ sa20;
        const unsigned h2 = (SA2LO * t + SK2LO) >> 16, h4 = (SA4LO * t + SK4LO) >> 16;
        if ((((t ^ sa32) ^ (cx32 * h2) ^ (cz32 * h4) ^ X32) & 15u) == 0u) {
            const unsigned s = atomicAdd(&sn, 1u);
            if (s < EARLY4_STAGE) sbuf[s] = t;
            else { const unsigned long long slot = atomicAdd(cnt, 1ULL); if (slot < cap) list[slot] = ((unsigned long long)task << 20) | t; }
        }
    }
    __syncthreads();
    const unsigned n = sn < EARLY4_STAGE ? sn : EARLY4_STAGE;
    if (threadIdx.x == 0) sbase = n ? atomicAdd(cnt, (unsigned long long)n) : 0ULL;
    __syncthreads();
    for (unsigned i = threadIdx.x; i < n; i += blockDim.x) { const unsigned long long slot = sbase + i; if (slot < cap) list[slot] = ((unsigned long long)task << 20) | sbuf[i]; }
}

__global__ void __launch_bounds__(128) solveLate3AKernel(const SolveTask* __restrict__ tasks, const unsigned long long* __restrict__ items, unsigned long long n,
                                                         SolveHit* __restrict__ out, unsigned long long* __restrict__ outCnt, unsigned long long cap) {
    const unsigned long long k = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (k >= n) return;
    const unsigned long long it = items[k];
    const unsigned task = (unsigned)(it >> 20);
    const SolveTask T = tasks[task];
    const unsigned X32 = (unsigned)T.X, cx32 = (unsigned)T.cx, cz32 = (unsigned)T.cz;
    unsigned long long t = it & 0xFFFFFULL;
    unsigned P = cx32 * (unsigned)(((SA2 * t + SK2)) >> 16), Q = cz32 * (unsigned)(((SA4 * t + SK4)) >> 16), S = (unsigned)(t ^ SA);
    const unsigned cxA = cx32 * SA2LO, czA = cz32 * SA4LO;
#pragma unroll
    for (int j = 20; j < 48; j++) {
        const int kb = j - 16;
        const unsigned bit = ((S ^ P ^ Q ^ X32) >> kb) & 1u;
        const unsigned msk = 0u - bit;
        P += msk & (cxA << kb); Q += msk & (czA << kb);
        if (j < 32) S ^= msk & (1u << j);
        t |= (unsigned long long)bit << j;
    }
    const unsigned long long Sfull = (t ^ SA) & S48_MASK;
    if (carverFull(Sfull, T.cx, T.cz) == T.X && (T.pad || hutExact(Sfull, T.Creg, T.cx & 31, T.cz & 31))) {
        const unsigned long long slot = atomicAdd(outCnt, 1ULL);
        if (slot < cap) { out[slot].S = Sfull; out[slot].X = T.X; out[slot].cx = T.cx; out[slot].cz = T.cz; out[slot].val = T.val; }
    }
}

__global__ void __launch_bounds__(128) solveLate3BKernel(const SolveTask* __restrict__ tasks, const unsigned long long* __restrict__ items, unsigned long long n,
                                                         SolveHit* __restrict__ out, unsigned long long* __restrict__ outCnt, unsigned long long cap) {
    const unsigned long long k = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (k >= n) return;
    const unsigned long long it = items[k];
    const unsigned task = (unsigned)(it >> 20);
    const SolveTask T = tasks[task];
    const unsigned X32 = (unsigned)T.X, cx32 = (unsigned)T.cx, cz32 = (unsigned)T.cz;
    const unsigned cxA = cx32 * SA2LO, czA = cz32 * SA4LO;
    unsigned long long t = it & 0xFFFFFULL;
    unsigned P = cx32 * (unsigned)(((SA2 * t + SK2)) >> 16), Q = cz32 * (unsigned)(((SA4 * t + SK4)) >> 16), S = (unsigned)(t ^ SA);
    int j = 20; unsigned pend = 0;
    for (;;) {
        bool descended = false;
        if (j == 48) {
            const unsigned long long Sfull = (t ^ SA) & S48_MASK;
            if (carverFull(Sfull, T.cx, T.cz) == T.X && (T.pad || hutExact(Sfull, T.Creg, T.cx & 31, T.cz & 31))) {
                const unsigned long long slot = atomicAdd(outCnt, 1ULL);
                if (slot < cap) { out[slot].S = Sfull; out[slot].X = T.X; out[slot].cx = T.cx; out[slot].cz = T.cz; out[slot].val = T.val; }
            }
        } else {
            const int kb = j - 16;
            if ((((S ^ P ^ Q ^ X32) >> kb) & 1u) == 0u) { pend |= 1u << (j - 20); j++; descended = true; }
            else {
                const unsigned P1 = P + (cxA << kb), Q1 = Q + (czA << kb), S1 = (j < 32) ? (S ^ (1u << j)) : S;
                if ((((S1 ^ P1 ^ Q1 ^ X32) >> kb) & 1u) == 0u) { t |= 1ULL << j; P = P1; Q = Q1; S = S1; j++; descended = true; }
            }
        }
        if (descended) continue;
        for (;;) {
            if (!pend) return;
            const int d = 31 - __clz(pend); pend &= ~(1u << d);
            const int jj = d + 20;
            const unsigned long long tt = t & ((1ULL << jj) - 1);
            const unsigned Pn = cx32 * (unsigned)(((SA2 * tt + SK2)) >> 16), Qn = cz32 * (unsigned)(((SA4 * tt + SK4)) >> 16), Sn = (unsigned)(tt ^ SA);
            const int kb = jj - 16;
            const unsigned P1 = Pn + (cxA << kb), Q1 = Qn + (czA << kb), S1 = (jj < 32) ? (Sn ^ (1u << jj)) : Sn;
            if ((((S1 ^ P1 ^ Q1 ^ X32) >> kb) & 1u) == 0u) { t = tt | (1ULL << jj); P = P1; Q = Q1; S = S1; j = jj + 1; break; }
        }
    }
}
