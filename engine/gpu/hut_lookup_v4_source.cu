#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <cmath>
#include <chrono>
#include <vector>
#include <algorithm>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <atomic>
#include <deque>
#include <string>

#include "device_noise.h"
#include "device_construct.h"
#include "room_core.h"
#include "swamphut.h"
#include "fused_gate_v5.cuh"

#define CUDA_OK(call) do { cudaError_t _e = (call); if (_e != cudaSuccess) { fprintf(stderr, "CUDA error %s at %s:%d\n", cudaGetErrorString(_e), __FILE__, __LINE__); exit(1); } } while (0)

#ifndef BLK
#define BLK 32
#endif
#define STRIP 64
#define S48_MASK 0xFFFFFFFFFFFFULL
#define S48_K    0x9E3779B97F4A7DULL
#define S48_SALT 0x2545F4914F6CDDULL

struct Match { int cx, cz; unsigned long long carver; unsigned val; };
struct Table { const unsigned long long* keys; const unsigned* vals; int bits; };

__device__ __forceinline__ unsigned tableFind(const Table& t, unsigned long long carver) {
    const unsigned long long key = carver | (1ULL << 48);
    unsigned h = (unsigned)((carver * 0x9E3779B97F4A7C15ULL) >> (64 - t.bits));
    const unsigned mask = (1u << t.bits) - 1;
    for (;;) {
        const unsigned long long k = t.keys[h];
        if (k == key) return t.vals[h];
        if (k == 0) return 0xFFFFFFFFu;
        h = (h + 1) & mask;
    }
}

__device__ __forceinline__ bool walkStepCarver(RoomWalker& w, const RoomCtx& c, uint64_t K, int32_t* cx, int32_t* cz, uint64_t* carver) {
    uint64_t s = (w.rs ^ RC_LCG_MULT) & RC_LCG_MASK;
    s = (s * RC_LCG_MULT + RC_LCG_ADD) & RC_LCG_MASK;
    const uint32_t b1 = (uint32_t)(s >> 17);
    s = (s * RC_LCG_MULT + RC_LCG_ADD) & RC_LCG_MASK;
    const uint32_t b2 = (uint32_t)(s >> 17);
    uint32_t v1, v2;
    const uint32_t bmax = b1 > b2 ? b1 : b2;
    if (bmax >= 0x7FFFFFE9U) {
        int32_t ex, ez;
        rcHutExact((int64_t)w.rs, w.rx, w.rz, &ex, &ez);
        v1 = (uint32_t)(ex - (int32_t)((uint32_t)w.rx * 32U));
        v2 = (uint32_t)(ez - (int32_t)((uint32_t)w.rz * 32U));
    } else {
        v1 = (uint32_t)rcMod24((int32_t)b1);
        v2 = (uint32_t)rcMod24((int32_t)b2);
    }
    const uint64_t X = w.px + (uint64_t)v1 * (uint64_t)c.a;
    const uint64_t Z = w.pz + (uint64_t)v2 * (uint64_t)c.b;
    const uint64_t s0 = ((X ^ Z) & RC_LCG_MASK) ^ K;
    const bool start = rcIsStartS0(c, s0);
    *carver = (s0 ^ RC_LCG_MULT) & RC_LCG_MASK;
    *cx = (int32_t)((uint32_t)w.rx * 32U + v1);
    *cz = (int32_t)((uint32_t)w.rz * 32U + v2);
    w.rs += (uint64_t)RC_RZ_MULT;
    w.pz += 32ULL * (uint64_t)c.b;
    w.rz += 1;
    return start;
}

__global__ void sweepProbeKernel(RoomCtx ctx, Table tab, int rxLo, int rzLo, int nRx, int nStrips, int nRz,
                                 unsigned long long* __restrict__ startCount, Match* __restrict__ out, unsigned long long* __restrict__ outCnt, unsigned long long outCap) {
    long long tid = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    long long threads = (long long)nRx * nStrips;
    if (tid >= threads) return;
    int rxIdx = (int)(tid % nRx), stripIdx = (int)(tid / nRx);
    int rx = rxLo + rxIdx, rz0 = stripIdx * STRIP, rzEnd = rz0 + STRIP < nRz ? rz0 + STRIP : nRz;
    unsigned long long localStarts = 0;
    RoomWalker w;
    rcWalkInit(w, ctx, rx, rzLo + rz0);
    const uint64_t K = rcWalkK(ctx);
    for (int i = rz0; i < rzEnd; i++) {
        int32_t cx, cz; uint64_t carver;
        if (!walkStepCarver(w, ctx, K, &cx, &cz, &carver)) continue;
        localStarts++;
        const unsigned v = tableFind(tab, carver);
        if (v == 0xFFFFFFFFu) continue;
        const unsigned long long slot = atomicAdd(outCnt, 1ULL);
        if (slot < outCap) { out[slot].cx = cx; out[slot].cz = cz; out[slot].carver = carver; out[slot].val = v; }
    }
    if (localStarts) atomicAdd(startCount, localStarts);
}

DN_HD inline bool footprintGate(const DevNoiseCache& c, int wx, int wz, int maxH, int ncols, bool swamp) {
    if (swamp && !dev_checkClimate(c, wx, wz, false)) return false;
    if (ncols == 1) return dev_checkColumn(c, wx, wz, maxH);
    const int fw = 7, fd = 9;
    const int xs[9] = { wx, wx + fw - 1, wx, wx + fw - 1, wx + fw / 2, wx + fw / 2, wx + fw / 2, wx, wx + fw - 1 };
    const int zs[9] = { wz, wz, wz + fd - 1, wz + fd - 1, wz + fd / 2, wz, wz + fd - 1, wz + fd / 2, wz + fd / 2 };
    if (ncols == 6) {
        const int ix6[6] = { 0, 1, 4, 5, 7, 8 };
        for (int i = 0; i < 6; i++) if (!dev_checkColumn(c, xs[ix6[i]] - 3, zs[ix6[i]] - 3, maxH)) return false;
        return true;
    }
    if (ncols < 0) { const int k = -ncols - 1; return dev_checkColumn(c, xs[k] - 3, zs[k] - 3, maxH); }
    const int n = ncols >= 9 ? 9 : 5;
    for (int i = 0; i < n; i++) if (!dev_checkColumn(c, xs[i] - 3, zs[i] - 3, maxH)) return false;
    return true;
}

__global__ void buildManyKernel(long long s48, int u0, int count, DevNoiseCache* __restrict__ caches) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= count) return;
    const long long ws = (long long)(((unsigned long long)(unsigned)(u0 + i) << 48) | (unsigned long long)s48);
    dev_buildWorldNoiseCache(ws, &caches[i]);
}

__global__ void gateKernel(const DevNoiseCache* __restrict__ caches, const Match* __restrict__ ms, unsigned long long Mn, unsigned long long pairs,
                           int maxH, int ncols, int swamp, unsigned long long* cnt, unsigned long long* out, unsigned long long cap) {
    unsigned long long tid = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (tid >= pairs) return;
    const unsigned long long ul = tid / Mn, mi = tid % Mn;
    const Match m = ms[mi];
    if (!footprintGate(caches[ul], 16 * m.cx, 16 * m.cz, maxH, ncols, swamp != 0)) return;
    const unsigned long long slot = atomicAdd(cnt, 1ULL);
    if (slot < cap) out[slot] = tid;
}

#define NPROF 16
__global__ void profKernel(const DevNoiseCache* __restrict__ caches, const Match* __restrict__ ms, unsigned long long Mn, unsigned long long pairs, int maxH, unsigned long long* cnt) {
    unsigned long long tid = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (tid >= pairs) return;
    const unsigned long long ul = tid / Mn, mi = tid % Mn;
    const Match m = ms[mi]; const DevNoiseCache& c = caches[ul];
    const int wx = 16 * m.cx, wz = 16 * m.cz, hx = wx, hz = wz;
    bool b[NPROF]; for (int i = 0; i < NPROF; i++) b[i] = false;
    b[0] = true;
    b[1] = dev_gateErosion(c, wx, wz);
    b[2] = dev_gateTemperature(c, wx, wz);
    b[3] = dev_gateRidge(c, wx, wz, false);
    b[4] = dev_gateContinentalness(c, wx, wz);
    b[5] = dev_Entrance(c, hx, 50, hz) < 0;
    b[6] = dev_Entrance(c, hx, 60, hz) < 0;
    b[7] = !(dev_Entrance2(c, hx, maxH, hz) >= 0 && dev_Cheese(c, hx, maxH, hz) >= 0);
    bool l = true;
    for (int y = 0; y >= maxH; y -= 10) if (maxH < y) { if (dev_Entrance2(c, hx, y, hz) >= 0 && dev_Cheese(c, hx, y, hz) >= 0) l = false; }
    b[8] = l;
    bool e = true;
    for (int y = 10; y <= 40; y += 10) if (dev_Entrance(c, hx, y, hz) >= 0 && dev_Cheese(c, hx, y, hz) >= 0) e = false;
    b[9] = e;
    bool f = true;
    for (int y = maxH; y <= 60; y += 10) if (dev_sample(c.aquiferFloodedness, hx, y * 0.67, hz) > 0.41) f = false;
    b[10] = f;
    for (int i = 0; i < 11; i++) if (b[i]) atomicAdd(&cnt[i], 1ULL);
    bool all = true; for (int i = 1; i < 11; i++) all = all && b[i];
    if (all) atomicAdd(&cnt[11], 1ULL);
    if (b[1] && b[2] && b[3] && b[4]) atomicAdd(&cnt[12], 1ULL);
    if (b[5] && b[6]) atomicAdd(&cnt[13], 1ULL);
    if (b[5] && b[6] && b[7] && b[8] && b[9] && b[10]) atomicAdd(&cnt[14], 1ULL);
}

__global__ void buildErosionKernel(long long s48, int u0, int count, DevNoiseCache* __restrict__ caches) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= count) return;
    const long long ws = (long long)(((unsigned long long)(unsigned)(u0 + i) << 48) | (unsigned long long)s48);
    Xoroshiro128PP base = Xoroshiro128PP::fromSingleSeed(ws);
    Xoroshiro128PP deriver = base.createRandomDeriver();
    const double amp[] = {1.0, 1.0, 0.0, 1.0, 1.0};
    dev_makeNamedSampler(deriver, DIGEST_erosion, -9, amp, 5, &caches[i].erosion);
}
__global__ void buildRestKernel(long long s48, int u0, const int* __restrict__ alive, int nAlive, DevNoiseCache* __restrict__ caches) {
    int k = blockIdx.x * blockDim.x + threadIdx.x;
    if (k >= nAlive) return;
    const int i = alive[k];
    const long long ws = (long long)(((unsigned long long)(unsigned)(u0 + i) << 48) | (unsigned long long)s48);
    Xoroshiro128PP base = Xoroshiro128PP::fromSingleSeed(ws);
    Xoroshiro128PP deriver = base.createRandomDeriver();
    { const double amp[] = {1.5, 0.0, 1.0, 0.0, 0.0, 0.0}; dev_makeNamedSampler(deriver, DIGEST_temperature, -10, amp, 6, &caches[i].temperature); }
    { const double amp[] = {1.0, 2.0, 1.0, 0.0, 0.0, 0.0}; dev_makeNamedSampler(deriver, DIGEST_ridge, -7, amp, 6, &caches[i].ridge); }
    dev_buildWorldNoiseCacheRemaining(ws, &caches[i]);
}

__global__ void erosionGateKernel(const DevNoiseCache* __restrict__ caches, const Match* __restrict__ ms, unsigned long long Mn, unsigned long long pairs,
                                  unsigned* __restrict__ list, unsigned long long* cnt, unsigned long long cap, unsigned char* __restrict__ uFlag) {
    unsigned long long tid = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (tid >= pairs) return;
    const unsigned long long ul = tid / Mn, mi = tid % Mn;
    if (!dev_gateErosion(caches[ul], 16 * ms[mi].cx, 16 * ms[mi].cz)) return;
    const unsigned long long slot = atomicAdd(cnt, 1ULL);
    if (slot < cap) list[slot] = (unsigned)tid;
    uFlag[ul] = 1;
}

__global__ void gateListKernel(const DevNoiseCache* __restrict__ caches, const Match* __restrict__ ms, unsigned long long Mn, const unsigned* __restrict__ list, unsigned long long n,
                               int maxH, int ncols, unsigned long long* cnt, unsigned long long* out, unsigned long long cap) {
    unsigned long long k = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (k >= n) return;
    const unsigned long long tid = list[k];
    const unsigned long long ul = tid / Mn, mi = tid % Mn;
    if (!footprintGate(caches[ul], 16 * ms[mi].cx, 16 * ms[mi].cz, maxH, ncols, true)) return;
    const unsigned long long slot = atomicAdd(cnt, 1ULL);
    if (slot < cap) out[slot] = tid;
}

__global__ void buildEroKernel(long long s48, int count, DevDoublePerlin* __restrict__ ero) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= count) return;
    const long long ws = (long long)(((unsigned long long)(unsigned)i << 48) | (unsigned long long)s48);
    Xoroshiro128PP base = Xoroshiro128PP::fromSingleSeed(ws);
    Xoroshiro128PP deriver = base.createRandomDeriver();
    const double amp[] = {1.0, 1.0, 0.0, 1.0, 1.0};
    dev_makeNamedSampler(deriver, DIGEST_erosion, -9, amp, 5, &ero[i]);
}

__device__ __forceinline__ void shPerlinCreate(Xoroshiro128PP& source, DevPerlin& d, unsigned* __restrict__ sh, int lane) {
    d.originX = source.nextDouble() * 256.0;
    d.originY = source.nextDouble() * 256.0;
    d.originZ = source.nextDouble() * 256.0;
    shShuffle256(source, sh, lane);
    d.iy0 = (int)floor(d.originY);
    d.fy0 = d.originY - d.iy0;
    unsigned* t = reinterpret_cast<unsigned*>(d.permutations);
#pragma unroll
    for (int w = 0; w < 64; w++) t[w] = sh[w * 32 + lane];
}
__device__ __forceinline__ void shBuildOctave(Xoroshiro128PP& random, int firstOctave, const double* amplitudes, int count, DevOctave* o, unsigned* sh, int lane) {
    o->octaveCount = count;
    o->lacunarity = pow(2.0, firstOctave);
    o->persistence = pow(2.0, count - 1) / (pow(2.0, count) - 1.0);
    Xoroshiro128PP octaveDeriver = random.createRandomDeriver();
    double lf = o->lacunarity, la = o->persistence;
    for (int i = 0; i < count; i++) {
        o->amplitudes[i] = amplitudes[i];
        o->hasOctave[i] = (amplitudes[i] != 0.0);
        o->freqAt[i] = lf; o->ampAt[i] = la;
        lf *= 2.0; la /= 2.0;
        if (o->hasOctave[i]) {
            Xoroshiro128PP per = Xoroshiro128PP::createRandomFromDigest(octaveDeriver, dev_octaveDigest(firstOctave + i));
            shPerlinCreate(per, o->samplers[i], sh, lane);
        }
    }
}
#define SH_BLK 64
__global__ void buildEroShKernel(long long s48, int count, DevDoublePerlin* __restrict__ ero) {
    __shared__ unsigned shAll[(SH_BLK / 32) * 64 * 32];
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= count) return;
    unsigned* sh = shAll + (threadIdx.x >> 5) * (64 * 32); const int lane = threadIdx.x & 31;
    const long long ws = (long long)(((unsigned long long)(unsigned)i << 48) | (unsigned long long)s48);
    Xoroshiro128PP base = Xoroshiro128PP::fromSingleSeed(ws);
    Xoroshiro128PP deriver = base.createRandomDeriver();
    const double amp[] = {1.0, 1.0, 0.0, 1.0, 1.0};
    Xoroshiro128PP r = Xoroshiro128PP::createRandomFromDigest(deriver, DIGEST_erosion);
    DevDoublePerlin* d = &ero[i];
    shBuildOctave(r, -9, amp, 5, &d->first, sh, lane);
    shBuildOctave(r, -9, amp, 5, &d->second, sh, lane);
    int minOctave = 2147483647, maxOctave = -2147483648;
    for (int q = 0; q < 5; q++) { if (amp[q] == 0.0) continue; if (q < minOctave) minOctave = q; if (q > maxOctave) maxOctave = q; }
    const double createAmp_n = maxOctave - minOctave;
    const double createAmplitude = 0.1 * (1.0 + 1.0 / (createAmp_n + 1.0));
    d->amplitude = 0.16666666666666666 / createAmplitude;
}

__device__ __forceinline__ void shBuildNamed(const Xoroshiro128PP& deriver, const uint8_t* digest, int firstOctave, const double* amp, int count, DevDoublePerlin* d, unsigned* sh, int lane) {
    Xoroshiro128PP r = Xoroshiro128PP::createRandomFromDigest(deriver, digest);
    shBuildOctave(r, firstOctave, amp, count, &d->first, sh, lane);
    shBuildOctave(r, firstOctave, amp, count, &d->second, sh, lane);
    int minOctave = 2147483647, maxOctave = -2147483648;
    for (int q = 0; q < count; q++) { if (amp[q] == 0.0) continue; if (q < minOctave) minOctave = q; if (q > maxOctave) maxOctave = q; }
    const double createAmp_n = maxOctave - minOctave;
    const double createAmplitude = 0.1 * (1.0 + 1.0 / (createAmp_n + 1.0));
    d->amplitude = 0.16666666666666666 / createAmplitude;
}

__device__ __forceinline__ void shEroOctaves(Xoroshiro128PP& octaveDeriver, DevOctave* o, int from, int to, bool meta, unsigned* sh, int lane) {
    const double amp[5] = {1.0, 1.0, 0.0, 1.0, 1.0};
    if (meta) {
        o->octaveCount = 5; o->lacunarity = pow(2.0, -9); o->persistence = pow(2.0, 4) / (pow(2.0, 5) - 1.0);
        double lf = o->lacunarity, la = o->persistence;
        for (int i = 0; i < 5; i++) { o->amplitudes[i] = amp[i]; o->hasOctave[i] = (amp[i] != 0.0); o->freqAt[i] = lf; o->ampAt[i] = la; lf *= 2.0; la /= 2.0; }
    }
    for (int i = from; i < to; i++)
        if (amp[i] != 0.0) { Xoroshiro128PP per = Xoroshiro128PP::createRandomFromDigest(octaveDeriver, dev_octaveDigest(-9 + i)); shPerlinCreate(per, o->samplers[i], sh, lane); }
}
__device__ __forceinline__ void eroDerivers(long long ws, Xoroshiro128PP& od1, Xoroshiro128PP& od2) {
    Xoroshiro128PP base = Xoroshiro128PP::fromSingleSeed(ws);
    Xoroshiro128PP deriver = base.createRandomDeriver();
    Xoroshiro128PP r = Xoroshiro128PP::createRandomFromDigest(deriver, DIGEST_erosion);
    od1 = r.createRandomDeriver();
    od2 = r.createRandomDeriver();
}
#define EROSION_AMP (0.16666666666666666 / (0.1 * (1.0 + 1.0 / (4.0 + 1.0))))
__global__ void stageA1Kernel(long long s48, const Match* __restrict__ m, DevDoublePerlin* __restrict__ ero, unsigned* __restrict__ L1, unsigned long long* __restrict__ n1) {
    __shared__ unsigned shAll[(SH_BLK / 32) * 64 * 32];
    const int u = blockIdx.x * blockDim.x + threadIdx.x;
    unsigned* sh = shAll + (threadIdx.x >> 5) * (64 * 32); const int lane = threadIdx.x & 31;
    const long long ws = (long long)(((unsigned long long)(unsigned)u << 48) | (unsigned long long)s48);
    Xoroshiro128PP od1, od2; eroDerivers(ws, od1, od2);
    DevDoublePerlin* d = &ero[u];
    shEroOctaves(od1, &d->first, 0, 2, true, sh, lane);
    shEroOctaves(od2, &d->second, 0, 2, true, sh, lane);
    d->amplitude = EROSION_AMP;
    const double f = 1.0181268882175227, X = (16 * m->cx + 8) / 4.0, Z = (16 * m->cz + 8) / 4.0;
    const double ap = dev_octaveRangeY0(d->first, X, Z, 0, DN_EROSION_SPLIT, 0.0), bp = dev_octaveRangeY0(d->second, X * f, Z * f, 0, DN_EROSION_SPLIT, 0.0);
    if ((ap + bp + dev_erosionTail(*d)) * d->amplitude < DN_EROSION_THRESHOLD - 1e-9) return;
    L1[atomicAdd(n1, 1ULL)] = (unsigned)u;
}
__global__ void stageA2Kernel(long long s48, const Match* __restrict__ m, DevDoublePerlin* __restrict__ ero, const unsigned* __restrict__ L1, unsigned long long nL1,
                              unsigned* __restrict__ L2, unsigned long long* __restrict__ n2) {
    __shared__ unsigned shAll[(SH_BLK / 32) * 64 * 32];
    const unsigned long long q = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (q >= nL1) return;
    unsigned* sh = shAll + (threadIdx.x >> 5) * (64 * 32); const int lane = threadIdx.x & 31;
    const unsigned u = L1[q];
    const long long ws = (long long)(((unsigned long long)u << 48) | (unsigned long long)s48);
    Xoroshiro128PP od1, od2; eroDerivers(ws, od1, od2);
    DevDoublePerlin* d = &ero[u];
    shEroOctaves(od1, &d->first, 2, 5, false, sh, lane);
    shEroOctaves(od2, &d->second, 2, 5, false, sh, lane);
    const double f = 1.0181268882175227, X = (16 * m->cx + 8) / 4.0, Z = (16 * m->cz + 8) / 4.0;
    const double ap = dev_octaveRangeY0(d->first, X, Z, 0, DN_EROSION_SPLIT, 0.0), bp = dev_octaveRangeY0(d->second, X * f, Z * f, 0, DN_EROSION_SPLIT, 0.0);
    const double a = dev_octaveRangeY0(d->first, X, Z, DN_EROSION_SPLIT, 5, ap), b = dev_octaveRangeY0(d->second, X * f, Z * f, DN_EROSION_SPLIT, 5, bp);
    if ((a + b) * d->amplitude < 0.55) return;
    L2[atomicAdd(n2, 1ULL)] = u;
}
__global__ void stageBKernel(long long s48, const Match* __restrict__ m, DevNoiseCache* __restrict__ caches, const unsigned* __restrict__ L2, unsigned long long nL2,
                             int* __restrict__ L3, unsigned long long* __restrict__ n3) {
    __shared__ unsigned shAll[(SH_BLK / 32) * 64 * 32];
    const unsigned long long k = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (k >= nL2) return;
    unsigned* sh = shAll + (threadIdx.x >> 5) * (64 * 32); const int lane = threadIdx.x & 31;
    const unsigned u = L2[k];
    const long long ws = (long long)(((unsigned long long)u << 48) | (unsigned long long)s48);
    Xoroshiro128PP base = Xoroshiro128PP::fromSingleSeed(ws);
    Xoroshiro128PP deriver = base.createRandomDeriver();
    DevNoiseCache* c = &caches[k];
    { const double amp[] = {0.4, 0.5, 1.0}; shBuildNamed(deriver, DIGEST_cave_entrance, -7, amp, 3, &c->caveEntrance, sh, lane); }
    { const double amp[] = {1.0}; shBuildNamed(deriver, DIGEST_spaghetti_3d_rarity, -11, amp, 1, &c->spaghettiRarity, sh, lane); }
    { const double amp[] = {1.0}; shBuildNamed(deriver, DIGEST_spaghetti_3d_thickness, -8, amp, 1, &c->spaghettiThickness, sh, lane); }
    { const double amp[] = {1.0}; shBuildNamed(deriver, DIGEST_spaghetti_3d_1, -7, amp, 1, &c->spaghetti3D1, sh, lane); }
    { const double amp[] = {1.0}; shBuildNamed(deriver, DIGEST_spaghetti_3d_2, -7, amp, 1, &c->spaghetti3D2, sh, lane); }
    { const double amp[] = {1.0}; shBuildNamed(deriver, DIGEST_spaghetti_roughness_modulator, -8, amp, 1, &c->spaghettiRoughnessModulator, sh, lane); }
    { const double amp[] = {1.0}; shBuildNamed(deriver, DIGEST_spaghetti_roughness, -5, amp, 1, &c->spaghettiRoughness, sh, lane); }
    const int wx = 16 * m->cx, wz = 16 * m->cz;
    const int px[6] = { wx, wx + 6, wx + 3, wx + 3, wx, wx + 6 }, pz[6] = { wz, wz, wz + 4, wz, wz + 4, wz + 4 };
    for (int i = 0; i < 6; i++) {
        if (dev_Entrance(*c, px[i], 50, pz[i]) >= 0) return;
        if (dev_Entrance(*c, px[i], 60, pz[i]) >= 0) return;
    }
    L3[atomicAdd(n3, 1ULL)] = (int)u;
}

__device__ __forceinline__ bool b1Test(long long s48, unsigned u, const Match* __restrict__ m, unsigned* sh, int lane, double* __restrict__ dbg, unsigned long long kk) {
    const long long ws = (long long)(((unsigned long long)u << 48) | (unsigned long long)s48);
    Xoroshiro128PP base = Xoroshiro128PP::fromSingleSeed(ws);
    Xoroshiro128PP deriver = base.createRandomDeriver();
    const int x = 16 * m->cx, z = 16 * m->cz;
    const int Y[2] = { 50, 60 };
    double PX[2], PY[2], PZ[2], v[2], cc[2], e[2], h[2], l[2], n3[2], md[2], rg[2];
    const double a1[1] = { 1.0 };

#pragma unroll
    for (int k = 0; k < 2; k++) { PX[k] = x * 0.75; PY[k] = Y[k] * 0.5; PZ[k] = z * 0.75; }
    { const double amp[] = { 0.4, 0.5, 1.0 }; fusedSample<2>(deriver, DIGEST_cave_entrance, -7, amp, 3, sh, lane, PX, PY, PZ, v); }
#pragma unroll
    for (int k = 0; k < 2; k++) cc[k] = v[k] + 0.37 + mh_clampedLerp(0.3, 0.0, (10 + (double)Y[k]) / 40.0);

#pragma unroll
    for (int k = 0; k < 2; k++) { PX[k] = x * 2.0; PY[k] = (double)Y[k]; PZ[k] = z * 2.0; }
    fusedSample<2>(deriver, DIGEST_spaghetti_3d_rarity, -11, a1, 1, sh, lane, PX, PY, PZ, v);
#pragma unroll
    for (int k = 0; k < 2; k++) e[k] = scaleTunnels(v[k]);

#pragma unroll
    for (int k = 0; k < 2; k++) { PX[k] = (double)x; PY[k] = (double)Y[k]; PZ[k] = (double)z; }
    fusedSample<2>(deriver, DIGEST_spaghetti_3d_thickness, -8, a1, 1, sh, lane, PX, PY, PZ, v);
#pragma unroll
    for (int k = 0; k < 2; k++) h[k] = pn_lerp((v[k] + 1.0) / 2.0, 0.065, 0.088);

    fusedSample<2>(deriver, DIGEST_spaghetti_roughness_modulator, -8, a1, 1, sh, lane, PX, PY, PZ, md);
    fusedSample<2>(deriver, DIGEST_spaghetti_roughness, -5, a1, 1, sh, lane, PX, PY, PZ, rg);

#pragma unroll
    for (int k = 0; k < 2; k++) { PX[k] = x / e[k]; PY[k] = Y[k] / e[k]; PZ[k] = z / e[k]; }
    fusedSample<2>(deriver, DIGEST_spaghetti_3d_1, -7, a1, 1, sh, lane, PX, PY, PZ, l);
    fusedSample<2>(deriver, DIGEST_spaghetti_3d_2, -7, a1, 1, sh, lane, PX, PY, PZ, n3);
    bool pass = true;
#pragma unroll
    for (int k = 0; k < 2; k++) {
        const double mm = fabs(e[k] * l[k]) - h[k];
        const double oo = fabs(e[k] * n3[k]) - h[k];
        const double pp = mh_clamp(mm > oo ? mm : oo, -1.0, 1.0);
        const double qq = (-0.05 + (-0.05 * md[k])) * (-0.4 + fabs(rg[k]));
        const double pq = pp + qq;
        const double ent = cc[k] < pq ? cc[k] : pq;
        if (dbg) dbg[(size_t)kk * 2 + k] = ent;
        if (ent >= B1_SLACK) pass = false;
    }
    return pass;
}
__global__ void __launch_bounds__(SH_BLK2) stageB1Kernel(long long s48, const Match* __restrict__ m, const unsigned* __restrict__ Lin, unsigned long long nIn,
                                                         unsigned* __restrict__ L2, unsigned long long* __restrict__ n2, double* __restrict__ dbg) {
    __shared__ unsigned shAll[(SH_BLK2 / 32) * 64 * 32];
    const unsigned long long kk = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (Lin ? kk >= nIn : kk >= 65536ULL) return;
    const unsigned u = Lin ? Lin[kk] : (unsigned)kk;
    unsigned* sh = shAll + (threadIdx.x >> 5) * (64 * 32); const int lane = threadIdx.x & 31;
    if (b1Test(s48, u, m, sh, lane, dbg, kk)) L2[atomicAdd(n2, 1ULL)] = u;
}

#define ERO_LAC (0.001953125)
__device__ __forceinline__ void eroGeom(const Match* m, double& X, double& Z) { X = (16 * m->cx + 8) / 4.0; Z = (16 * m->cz + 8) / 4.0; }
__device__ __forceinline__ double eroPer() { return pow(2.0, 4) / (pow(2.0, 5) - 1.0); }
__device__ __forceinline__ double eroAcc(const Xoroshiro128PP& od, double xs, double zs, int from, int to, double acc, unsigned* sh, int lane) {
    double lf = ERO_LAC, la = eroPer();
    for (int i = 0; i < from; i++) { lf *= 2.0; la /= 2.0; }
    for (int i = from; i < to; i++) {
        if (i != 2) {
            Xoroshiro128PP r = Xoroshiro128PP::createRandomFromDigest(od, dev_octaveDigest(-9 + i));
            ShPerm p; shTableBuild(r, sh, lane, p);
#ifdef __CUDA_ARCH__
            const unsigned long long d = dn_bits(lf) - 0x3FF0000000000000ULL;
            const double sx = dn_from_bits(dn_bits(xs) + d), sz = dn_from_bits(dn_bits(zs) + d);
            acc += 1.0 * perlinShY0(p, sx, sz) * la;
#endif
        }
        lf *= 2.0; la /= 2.0;
    }
    return acc;
}
__global__ void __launch_bounds__(SH_BLK2) stageA1fKernel(long long s48, const Match* __restrict__ m, double* __restrict__ apb, unsigned* __restrict__ L1, unsigned long long* __restrict__ n1) {
    __shared__ unsigned shAll[(SH_BLK2 / 32) * 64 * 32];
    const unsigned u = blockIdx.x * blockDim.x + threadIdx.x;
    unsigned* sh = shAll + (threadIdx.x >> 5) * (64 * 32); const int lane = threadIdx.x & 31;
    const long long ws = (long long)(((unsigned long long)u << 48) | (unsigned long long)s48);
    Xoroshiro128PP od1, od2; eroDerivers(ws, od1, od2);
    const double f = 1.0181268882175227; double X, Z; eroGeom(m, X, Z);
    const double ap = eroAcc(od1, X, Z, 0, 2, 0.0, sh, lane), bp = eroAcc(od2, X * f, Z * f, 0, 2, 0.0, sh, lane);
    const double per = eroPer();
    double t = 0.0; t += 1.0 * (per / 8.0) * DN_PERLIN_ABS_MAX; t += 1.0 * (per / 16.0) * DN_PERLIN_ABS_MAX;
    if ((ap + bp + (t + t)) * EROSION_AMP < DN_EROSION_THRESHOLD - 1e-9) return;
    apb[2 * (size_t)u] = ap; apb[2 * (size_t)u + 1] = bp;
    L1[atomicAdd(n1, 1ULL)] = u;
}
__global__ void __launch_bounds__(SH_BLK2) stageA2fKernel(long long s48, const Match* __restrict__ m, const double* __restrict__ apb, const unsigned* __restrict__ L1, const unsigned long long* __restrict__ nL1p, unsigned long long nCap,
                                                          unsigned* __restrict__ L2, unsigned long long* __restrict__ n2) {
    __shared__ unsigned shAll[(SH_BLK2 / 32) * 64 * 32];
    const unsigned long long q = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (q >= (nL1p ? (*nL1p < nCap ? *nL1p : nCap) : nCap)) return;
    unsigned* sh = shAll + (threadIdx.x >> 5) * (64 * 32); const int lane = threadIdx.x & 31;
    const unsigned u = L1[q];
    const long long ws = (long long)(((unsigned long long)u << 48) | (unsigned long long)s48);
    Xoroshiro128PP od1, od2; eroDerivers(ws, od1, od2);
    const double f = 1.0181268882175227; double X, Z; eroGeom(m, X, Z);
    const double a = eroAcc(od1, X, Z, 2, 5, apb[2 * (size_t)u], sh, lane), b = eroAcc(od2, X * f, Z * f, 2, 5, apb[2 * (size_t)u + 1], sh, lane);
    if ((a + b) * EROSION_AMP < 0.55 - 1e-12) return;
    L2[atomicAdd(n2, 1ULL)] = u;
}

__global__ void gate12Kernel(const DevNoiseCache* __restrict__ caches, const unsigned* __restrict__ ulist, const unsigned long long* __restrict__ nPtr, unsigned long long nCap, const Match* __restrict__ m,
                             unsigned* __restrict__ L3u, int* __restrict__ L3i, unsigned long long* __restrict__ n3) {
    const unsigned long long k = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (k >= (nPtr ? (*nPtr < nCap ? *nPtr : nCap) : nCap)) return;
    const DevNoiseCache& c = caches[k];
    const int wx = 16 * m->cx, wz = 16 * m->cz;
    const int px[6] = { wx, wx + 6, wx + 3, wx + 3, wx, wx + 6 }, pz[6] = { wz, wz, wz + 4, wz, wz + 4, wz + 4 };
    for (int i = 0; i < 6; i++) {
        if (dev_Entrance(c, px[i], 50, pz[i]) >= 0) return;
        if (dev_Entrance(c, px[i], 60, pz[i]) >= 0) return;
    }
    const unsigned long long s = atomicAdd(n3, 1ULL);
    L3u[s] = ulist[k]; L3i[s] = (int)k;
}
__global__ void finalGate2Kernel(const DevNoiseCache* __restrict__ caches, const int* __restrict__ L3i, const unsigned* __restrict__ L3u, const unsigned long long* __restrict__ nPtr, unsigned long long nCap, const Match* __restrict__ m, int maxH, int ncols,
                                 unsigned long long* cnt, unsigned long long* out, unsigned long long cap, bool swamp) {
    const int k = blockIdx.x * blockDim.x + threadIdx.x;
    if ((unsigned long long)k >= (nPtr ? (*nPtr < nCap ? *nPtr : nCap) : nCap)) return;
    if (!footprintGate(caches[L3i[k]], 16 * m->cx, 16 * m->cz, maxH, ncols, swamp)) return;
    const unsigned long long slot = atomicAdd(cnt, 1ULL);
    if (slot < cap) out[slot] = (unsigned long long)L3u[k];
}

__constant__ double c_eroC0, c_eroC1;
__global__ void __launch_bounds__(SH_BLK2) stageA0bKernel(const long long* __restrict__ s48s, const Match* __restrict__ ms, double* __restrict__ apb, unsigned* __restrict__ L0, unsigned long long* __restrict__ n0) {
    __shared__ unsigned shAll[(SH_BLK2 / 32) * 64 * 32];
    const unsigned u = blockIdx.x * blockDim.x + threadIdx.x, spot = blockIdx.y;
    unsigned* sh = shAll + (threadIdx.x >> 5) * (64 * 32); const int lane = threadIdx.x & 31;
    const Match* m = ms + spot;
    const long long ws = (long long)(((unsigned long long)u << 48) | (unsigned long long)s48s[spot]);
    Xoroshiro128PP od1, od2; eroDerivers(ws, od1, od2);
    const double f = 1.0181268882175227; double X, Z; eroGeom(m, X, Z);
    const double ap = eroAcc(od1, X, Z, 0, 1, 0.0, sh, lane), bp = eroAcc(od2, X * f, Z * f, 0, 1, 0.0, sh, lane);
    if ((ap + bp) * EROSION_AMP < c_eroC0) return;
    const size_t ei = ((size_t)spot << 16) | u;
    apb[2 * ei] = ap; apb[2 * ei + 1] = bp;
    L0[atomicAdd(n0, 1ULL)] = (spot << 16) | u;
}
__global__ void __launch_bounds__(SH_BLK2) stageA1pbKernel(const long long* __restrict__ s48s, const Match* __restrict__ ms, double* __restrict__ apb, const unsigned* __restrict__ L0, const unsigned long long* __restrict__ nL0p,
                                                           unsigned long long nCap, unsigned* __restrict__ L1, unsigned long long* __restrict__ n1) {
    __shared__ unsigned shAll[(SH_BLK2 / 32) * 64 * 32];
    const unsigned long long q = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (q >= (*nL0p < nCap ? *nL0p : nCap)) return;
    unsigned* sh = shAll + (threadIdx.x >> 5) * (64 * 32); const int lane = threadIdx.x & 31;
    const unsigned e = L0[q], spot = e >> 16, u = e & 0xFFFFu;
    const Match* m = ms + spot;
    const long long ws = (long long)(((unsigned long long)u << 48) | (unsigned long long)s48s[spot]);
    Xoroshiro128PP od1, od2; eroDerivers(ws, od1, od2);
    const double f = 1.0181268882175227; double X, Z; eroGeom(m, X, Z);
    const size_t ei = ((size_t)spot << 16) | u;
    const double ap = eroAcc(od1, X, Z, 1, 2, apb[2 * ei], sh, lane), bp = eroAcc(od2, X * f, Z * f, 1, 2, apb[2 * ei + 1], sh, lane);
    if ((ap + bp) * EROSION_AMP < c_eroC1) return;
    apb[2 * ei] = ap; apb[2 * ei + 1] = bp;
    L1[atomicAdd(n1, 1ULL)] = e;
}

__constant__ double c_eroT1, c_eroT3;
__global__ void __launch_bounds__(SH_BLK2) stageA0aKernel(const long long* __restrict__ s48s, const Match* __restrict__ ms, double* __restrict__ apb, unsigned* __restrict__ Lx, unsigned long long* __restrict__ nx) {
    __shared__ unsigned shAll[(SH_BLK2 / 32) * 64 * 32];
    const unsigned u = blockIdx.x * blockDim.x + threadIdx.x, spot = blockIdx.y;
    unsigned* sh = shAll + (threadIdx.x >> 5) * (64 * 32); const int lane = threadIdx.x & 31;
    const Match* m = ms + spot;
    const long long ws = (long long)(((unsigned long long)u << 48) | (unsigned long long)s48s[spot]);
    Xoroshiro128PP od1, od2; eroDerivers(ws, od1, od2);
    double X, Z; eroGeom(m, X, Z);
    const double ap = eroAcc(od1, X, Z, 0, 1, 0.0, sh, lane);
    if (ap * EROSION_AMP < c_eroT1) return;
    const size_t ei = ((size_t)spot << 16) | u;
    apb[2 * ei] = ap;
    Lx[atomicAdd(nx, 1ULL)] = (spot << 16) | u;
}
__global__ void __launch_bounds__(SH_BLK2) stageA0cKernel(const long long* __restrict__ s48s, const Match* __restrict__ ms, double* __restrict__ apb, const unsigned* __restrict__ Lx, const unsigned long long* __restrict__ nxp,
                                                          unsigned long long nCap, unsigned* __restrict__ L0, unsigned long long* __restrict__ n0) {
    __shared__ unsigned shAll[(SH_BLK2 / 32) * 64 * 32];
    const unsigned long long q = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (q >= (*nxp < nCap ? *nxp : nCap)) return;
    unsigned* sh = shAll + (threadIdx.x >> 5) * (64 * 32); const int lane = threadIdx.x & 31;
    const unsigned e = Lx[q], spot = e >> 16, u = e & 0xFFFFu;
    const Match* m = ms + spot;
    const long long ws = (long long)(((unsigned long long)u << 48) | (unsigned long long)s48s[spot]);
    Xoroshiro128PP od1, od2; eroDerivers(ws, od1, od2);
    const double f = 1.0181268882175227; double X, Z; eroGeom(m, X, Z);
    const size_t ei = ((size_t)spot << 16) | u;
    const double bp = eroAcc(od2, X * f, Z * f, 0, 1, 0.0, sh, lane);
    if ((apb[2 * ei] + bp) * EROSION_AMP < c_eroC0) return;
    apb[2 * ei + 1] = bp;
    L0[atomicAdd(n0, 1ULL)] = e;
}
__global__ void __launch_bounds__(SH_BLK2) stageA1aKernel(const long long* __restrict__ s48s, const Match* __restrict__ ms, double* __restrict__ apb, const unsigned* __restrict__ L0, const unsigned long long* __restrict__ nL0p,
                                                          unsigned long long nCap, unsigned* __restrict__ Ly, unsigned long long* __restrict__ ny) {
    __shared__ unsigned shAll[(SH_BLK2 / 32) * 64 * 32];
    const unsigned long long q = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (q >= (*nL0p < nCap ? *nL0p : nCap)) return;
    unsigned* sh = shAll + (threadIdx.x >> 5) * (64 * 32); const int lane = threadIdx.x & 31;
    const unsigned e = L0[q], spot = e >> 16, u = e & 0xFFFFu;
    const Match* m = ms + spot;
    const long long ws = (long long)(((unsigned long long)u << 48) | (unsigned long long)s48s[spot]);
    Xoroshiro128PP od1, od2; eroDerivers(ws, od1, od2);
    double X, Z; eroGeom(m, X, Z);
    const size_t ei = ((size_t)spot << 16) | u;
    const double ap = eroAcc(od1, X, Z, 1, 2, apb[2 * ei], sh, lane);
    if ((ap + apb[2 * ei + 1]) * EROSION_AMP < c_eroT3) return;
    apb[2 * ei] = ap;
    Ly[atomicAdd(ny, 1ULL)] = e;
}
__global__ void __launch_bounds__(SH_BLK2) stageA1cKernel(const long long* __restrict__ s48s, const Match* __restrict__ ms, double* __restrict__ apb, const unsigned* __restrict__ Ly, const unsigned long long* __restrict__ nyp,
                                                          unsigned long long nCap, unsigned* __restrict__ L1, unsigned long long* __restrict__ n1) {
    __shared__ unsigned shAll[(SH_BLK2 / 32) * 64 * 32];
    const unsigned long long q = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (q >= (*nyp < nCap ? *nyp : nCap)) return;
    unsigned* sh = shAll + (threadIdx.x >> 5) * (64 * 32); const int lane = threadIdx.x & 31;
    const unsigned e = Ly[q], spot = e >> 16, u = e & 0xFFFFu;
    const Match* m = ms + spot;
    const long long ws = (long long)(((unsigned long long)u << 48) | (unsigned long long)s48s[spot]);
    Xoroshiro128PP od1, od2; eroDerivers(ws, od1, od2);
    const double f = 1.0181268882175227; double X, Z; eroGeom(m, X, Z);
    const size_t ei = ((size_t)spot << 16) | u;
    const double bp = eroAcc(od2, X * f, Z * f, 1, 2, apb[2 * ei + 1], sh, lane);
    if ((apb[2 * ei] + bp) * EROSION_AMP < c_eroC1) return;
    apb[2 * ei + 1] = bp;
    L1[atomicAdd(n1, 1ULL)] = e;
}
__global__ void __launch_bounds__(SH_BLK2) stageA1bKernel(const long long* __restrict__ s48s, const Match* __restrict__ ms, double* __restrict__ apb, unsigned* __restrict__ L1, unsigned long long* __restrict__ n1) {
    __shared__ unsigned shAll[(SH_BLK2 / 32) * 64 * 32];
    const unsigned u = blockIdx.x * blockDim.x + threadIdx.x, spot = blockIdx.y;
    unsigned* sh = shAll + (threadIdx.x >> 5) * (64 * 32); const int lane = threadIdx.x & 31;
    const Match* m = ms + spot;
    const long long ws = (long long)(((unsigned long long)u << 48) | (unsigned long long)s48s[spot]);
    Xoroshiro128PP od1, od2; eroDerivers(ws, od1, od2);
    const double f = 1.0181268882175227; double X, Z; eroGeom(m, X, Z);
    const double ap = eroAcc(od1, X, Z, 0, 2, 0.0, sh, lane), bp = eroAcc(od2, X * f, Z * f, 0, 2, 0.0, sh, lane);
    const double per = eroPer();
    double t = 0.0; t += 1.0 * (per / 8.0) * DN_PERLIN_ABS_MAX; t += 1.0 * (per / 16.0) * DN_PERLIN_ABS_MAX;
    if ((ap + bp + (t + t)) * EROSION_AMP < DN_EROSION_THRESHOLD - 1e-9) return;
    const size_t ei = ((size_t)spot << 16) | u;
    apb[2 * ei] = ap; apb[2 * ei + 1] = bp;
    L1[atomicAdd(n1, 1ULL)] = (spot << 16) | u;
}
__global__ void __launch_bounds__(SH_BLK2) stageA2bKernel(const long long* __restrict__ s48s, const Match* __restrict__ ms, const double* __restrict__ apb, const unsigned* __restrict__ L1, const unsigned long long* __restrict__ nL1p,
                                                          unsigned long long nCap, unsigned* __restrict__ L2, unsigned long long* __restrict__ n2) {
    __shared__ unsigned shAll[(SH_BLK2 / 32) * 64 * 32];
    const unsigned long long q = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (q >= (*nL1p < nCap ? *nL1p : nCap)) return;
    unsigned* sh = shAll + (threadIdx.x >> 5) * (64 * 32); const int lane = threadIdx.x & 31;
    const unsigned e = L1[q], spot = e >> 16, u = e & 0xFFFFu;
    const Match* m = ms + spot;
    const long long ws = (long long)(((unsigned long long)u << 48) | (unsigned long long)s48s[spot]);
    Xoroshiro128PP od1, od2; eroDerivers(ws, od1, od2);
    const double f = 1.0181268882175227; double X, Z; eroGeom(m, X, Z);
    const size_t ei = ((size_t)spot << 16) | u;
    const double a = eroAcc(od1, X, Z, 2, 5, apb[2 * ei], sh, lane), b = eroAcc(od2, X * f, Z * f, 2, 5, apb[2 * ei + 1], sh, lane);
    if ((a + b) * EROSION_AMP < 0.55 - 1e-12) return;
    L2[atomicAdd(n2, 1ULL)] = e;
}

__global__ void __launch_bounds__(SH_BLK2) stageB1bKernel(const long long* __restrict__ s48s, const Match* __restrict__ ms, const unsigned* __restrict__ Lin, const unsigned long long* __restrict__ nInPtr, unsigned long long nCap,
                                                          unsigned* __restrict__ L2, unsigned long long* __restrict__ n2) {
    __shared__ unsigned shAll[(SH_BLK2 / 32) * 64 * 32];
    const unsigned long long kk = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
    unsigned e;
    if (Lin) { if (kk >= (*nInPtr < nCap ? *nInPtr : nCap)) return; e = Lin[kk]; }
    else { if (kk >= 65536ULL) return; e = (blockIdx.y << 16) | (unsigned)kk; }
    unsigned* sh = shAll + (threadIdx.x >> 5) * (64 * 32); const int lane = threadIdx.x & 31;
    const unsigned spot = e >> 16, u = e & 0xFFFFu;
    if (b1Test(s48s[spot], u, ms + spot, sh, lane, nullptr, kk)) L2[atomicAdd(n2, 1ULL)] = e;
}
__global__ void gate12bKernel(const DevNoiseCache* __restrict__ caches, const unsigned* __restrict__ elist, const unsigned long long* __restrict__ nPtr, unsigned long long nCap, const Match* __restrict__ ms,
                              unsigned* __restrict__ L3e, int* __restrict__ L3i, unsigned long long* __restrict__ n3) {
    const unsigned long long k = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (k >= (*nPtr < nCap ? *nPtr : nCap)) return;
    const DevNoiseCache& c = caches[k];
    const Match* m = ms + (elist[k] >> 16);
    const int wx = 16 * m->cx, wz = 16 * m->cz;
    const int px[6] = { wx, wx + 6, wx + 3, wx + 3, wx, wx + 6 }, pz[6] = { wz, wz, wz + 4, wz, wz + 4, wz + 4 };
    for (int i = 0; i < 6; i++) {
        if (dev_Entrance(c, px[i], 50, pz[i]) >= 0) return;
        if (dev_Entrance(c, px[i], 60, pz[i]) >= 0) return;
    }
    const unsigned long long s = atomicAdd(n3, 1ULL);
    L3e[s] = elist[k]; L3i[s] = (int)k;
}
__global__ void finalGate2bKernel(const DevNoiseCache* __restrict__ caches, const int* __restrict__ L3i, const unsigned* __restrict__ L3e, const unsigned long long* __restrict__ nPtr, unsigned long long nCap, const Match* __restrict__ ms,
                                  int maxH, int ncols, unsigned long long* cnt, unsigned long long* out, unsigned long long cap, bool swamp) {
    const unsigned long long k = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (k >= (*nPtr < nCap ? *nPtr : nCap)) return;
    const Match* m = ms + (L3e[k] >> 16);
    if (!footprintGate(caches[L3i[k]], 16 * m->cx, 16 * m->cz, maxH, ncols, swamp)) return;
    const unsigned long long slot = atomicAdd(cnt, 1ULL);
    if (slot < cap) out[slot] = (unsigned long long)L3e[k];
}
__global__ void finalGate1Kernel(const DevNoiseCache* __restrict__ caches, const int* __restrict__ L3, int n, const Match* __restrict__ m, int maxH, int ncols,
                                 unsigned long long* cnt, unsigned long long* out, unsigned long long cap, bool swamp = true) {
    const int k = blockIdx.x * blockDim.x + threadIdx.x;
    if (k >= n) return;
    if (!footprintGate(caches[k], 16 * m->cx, 16 * m->cz, maxH, ncols, swamp)) return;
    const unsigned long long slot = atomicAdd(cnt, 1ULL);
    if (slot < cap) out[slot] = (unsigned long long)L3[k];
}

__global__ void eroGateKernel(const DevDoublePerlin* __restrict__ ero, const Match* __restrict__ ms, unsigned long long Mn, unsigned long long pairs,
                              unsigned* __restrict__ list, unsigned long long* cnt, unsigned long long cap, unsigned char* __restrict__ uFlag) {
    unsigned long long tid = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (tid >= pairs) return;
    const unsigned long long ul = tid / Mn, mi = tid % Mn;
    if (dev_sampleY0(ero[ul], (16 * ms[mi].cx + 8) / 4.0, (16 * ms[mi].cz + 8) / 4.0) < 0.55) return;
    const unsigned long long slot = atomicAdd(cnt, 1ULL);
    if (slot < cap) list[slot] = (unsigned)tid;
    uFlag[ul] = 1;
}
__global__ void buildAliveKernel(long long s48, const int* __restrict__ alive, int n, DevNoiseCache* __restrict__ caches) {
    int k = blockIdx.x * blockDim.x + threadIdx.x;
    if (k >= n) return;
    const long long ws = (long long)(((unsigned long long)(unsigned)alive[k] << 48) | (unsigned long long)s48);
    dev_buildWorldNoiseCache(ws, &caches[k]);
}

__global__ void buildAliveSkyKernel(long long s48, const int* __restrict__ alive, int n, DevNoiseCache* __restrict__ caches) {
    int k = blockIdx.x * blockDim.x + threadIdx.x;
    if (k >= n) return;
    const long long ws = (long long)(((unsigned long long)(unsigned)alive[k] << 48) | (unsigned long long)s48);
    DevNoiseCache* c = &caches[k];
    Xoroshiro128PP base = Xoroshiro128PP::fromSingleSeed(ws);
    Xoroshiro128PP deriver = base.createRandomDeriver();
    { const double amp[] = {0.4, 0.5, 1.0}; dev_makeNamedSampler(deriver, DIGEST_cave_entrance, -7, amp, 3, &c->caveEntrance); }
    { const double amp[] = {1.0}; dev_makeNamedSampler(deriver, DIGEST_spaghetti_3d_rarity, -11, amp, 1, &c->spaghettiRarity); }
    { const double amp[] = {1.0}; dev_makeNamedSampler(deriver, DIGEST_spaghetti_3d_thickness, -8, amp, 1, &c->spaghettiThickness); }
    { const double amp[] = {1.0}; dev_makeNamedSampler(deriver, DIGEST_spaghetti_3d_1, -7, amp, 1, &c->spaghetti3D1); }
    { const double amp[] = {1.0}; dev_makeNamedSampler(deriver, DIGEST_spaghetti_3d_2, -7, amp, 1, &c->spaghetti3D2); }
    { const double amp[] = {1.0}; dev_makeNamedSampler(deriver, DIGEST_spaghetti_roughness_modulator, -8, amp, 1, &c->spaghettiRoughnessModulator); }
    { const double amp[] = {1.0}; dev_makeNamedSampler(deriver, DIGEST_spaghetti_roughness, -5, amp, 1, &c->spaghettiRoughness); }
    Xoroshiro128PP cheeseBase = Xoroshiro128PP::fromSingleSeed(ws);
    Xoroshiro128PP cheeseDeriver = cheeseBase.createRandomDeriver();
    { const double amp[] = {1.0}; dev_makeNamedSampler(cheeseDeriver, DIGEST_cave_layer, -8, amp, 1, &c->caveLayer); }
    { const double amp[] = {0.5, 1.0, 2.0, 1.0, 2.0, 1.0, 0.0, 2.0, 0.0}; dev_makeNamedSampler(cheeseDeriver, DIGEST_cave_cheese, -8, amp, 9, &c->caveCheese); }
    Xoroshiro128PP aqBase = Xoroshiro128PP::fromSingleSeed(ws);
    Xoroshiro128PP aqDeriver = aqBase.createRandomDeriver();
    { const double amp[] = {1.0}; dev_makeNamedSampler(aqDeriver, DIGEST_aquifer_fluid_level_floodedness, -7, amp, 1, &c->aquiferFloodedness); }
}
__global__ void gateListKernel2(const DevNoiseCache* __restrict__ caches, const int* __restrict__ uIdx, const Match* __restrict__ ms, unsigned long long Mn, const unsigned* __restrict__ list,
                                unsigned long long n, int maxH, int ncols, unsigned long long* cnt, unsigned long long* out, unsigned long long cap) {
    unsigned long long k = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (k >= n) return;
    const unsigned long long tid = list[k];
    const unsigned long long ul = tid / Mn, mi = tid % Mn;
    if (!footprintGate(caches[uIdx[ul]], 16 * ms[mi].cx, 16 * ms[mi].cz, maxH, ncols, true)) return;
    const unsigned long long slot = atomicAdd(cnt, 1ULL);
    if (slot < cap) out[slot] = tid;
}

#define INV_TILE 512
__global__ void invertKernel(RoomCtx ctx, const unsigned long long* __restrict__ lx, const unsigned* __restrict__ lv, int L, int kz, unsigned long long binv, unsigned long long mbits,
                             int rxLo, int nRx, int radius, Match* __restrict__ out, unsigned long long* __restrict__ outCnt, unsigned long long outCap) {
    __shared__ unsigned long long sY[INV_TILE];
    const long long tid = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    const bool act = tid < (long long)nRx * 24;
    const int rx = rxLo + (int)(tid / 24), ox = (int)(tid % 24);
    const int cx = (int)((uint32_t)rx * 32U + (uint32_t)ox);
    const unsigned long long S = (unsigned long long)ctx.seed & S48_MASK;
    const unsigned long long ca = act ? (((unsigned long long)((long long)cx * ctx.a)) & S48_MASK) : 0ULL;
    const unsigned long long LIM = 1874999ULL;
    for (int t0 = 0; t0 < L; t0 += INV_TILE) {
        const int tn = min(INV_TILE, L - t0);
        __syncthreads();
        for (int j = threadIdx.x; j < tn; j += blockDim.x) sY[j] = (lx[t0 + j] ^ S) & S48_MASK;
        __syncthreads();
        if (!act) continue;
        for (int j = 0; j < tn; j++) {
            const unsigned long long rhs = sY[j] ^ ca;
            const unsigned long long v = ((rhs >> kz) * binv) & mbits;
            if (((v + LIM) & mbits) > 2 * LIM) continue;
            const long long cz = (v >> (47 - kz)) ? (long long)v - (long long)(mbits + 1) : (long long)v;
            const unsigned long long X = lx[t0 + j];
            const unsigned long long chk = (((unsigned long long)((long long)cx * ctx.a)) ^ ((unsigned long long)(cz * ctx.b)) ^ (unsigned long long)ctx.seed) & S48_MASK;
            if (chk != X) continue;
            if (!rcIsStartSeed(ctx, (int64_t)X)) continue;
            const int rz = (int)(cz >> 5);
            if (rz < -radius || rz > radius) continue;
            int32_t ex, ez;
            rcHutExact(rcRowBase(ctx.seed, rx, rz), rx, rz, &ex, &ez);
            if (ex != cx || ez != (int32_t)cz) continue;
            const unsigned long long slot = atomicAdd(outCnt, 1ULL);
            if (slot < outCap) { out[slot].cx = cx; out[slot].cz = (int)cz; out[slot].carver = X; out[slot].val = lv[t0 + j]; }
        }
    }
}
#include "solver_kernels.cuh"
#include "solver_fused.cuh"

static unsigned long long invOdd(unsigned long long b) { unsigned long long x = b; for (int i = 0; i < 6; i++) x *= 2ULL - b * x; return x; }

static long long pickRegion(int regime, unsigned long long h, int radius) {
    long long lo[2], hi[2]; int n;
    if (regime == 0)      { lo[0] = -16384;  hi[0] = 16383;  n = 1; }
    else if (regime == 1) { lo[0] = -32768;  hi[0] = -16385; lo[1] = 16384; hi[1] = 32767; n = 2; }
    else                  { lo[0] = -radius; hi[0] = -32769; lo[1] = 32768; hi[1] = radius; n = 2; }
    unsigned long long tot = 0; for (int i = 0; i < n; i++) tot += (unsigned long long)(hi[i] - lo[i] + 1);
    unsigned long long k = h % tot;
    for (int i = 0; i < n; i++) { const unsigned long long w = (unsigned long long)(hi[i] - lo[i] + 1); if (k < w) return lo[i] + (long long)k; k -= w; }
    return 0;
}

static RoomCtx makeCtx(int64_t seed) {
    JavaRandom r; r.setSeed(seed);
    RoomCtx c; c.seed = seed; c.a = javaNextLong(r); c.b = javaNextLong(r);
    c.hiReject = (int32_t)ceil(RC_MINESHAFT_P * 67108864.0);
    c.hiAcceptBelow = (int32_t)floor(RC_MINESHAFT_P * 67108864.0);
    return c;
}

static double tB1v = 0; static unsigned long long totN2f = 0, totN3f = 0;
static double tHA1 = 0, tHA2 = 0, tHB = 0, tHC = 0; static unsigned long long totH1 = 0, totH2 = 0;
int main(int argc, char** argv) {
    const char* catPath = nullptr; const char* outPath = "hut_cands.csv"; const char* spotsPath = nullptr; int regX = -1, regZ = -1;
    long long firstIndex = 0, count = 1; int minAir = 0, maxH = -54, ncols = 6, uLo = 0, uHi = 65536, uBatch = 4096; bool noFuse = false, noPitStage = false, swamp = true, quiet = false, profile = false, timing = false, invert = false, noStage = false, solve = false; int minLo = 0, taskBatch = 8192; unsigned long long oneX = 0; long long oneCx = 0, oneCz = 0; bool one = false, solveOnly = false, noHut = false, noFast = false, noSh = false, noS1 = false, solveOld = false, solve3 = false, solve4 = false, noAsync = false, oddTasks = false; int batchReq = -1; FILE* dumpHits = nullptr, *dumpTasks = nullptr, *dumpLists = nullptr;
    int itemCapMi = 24; bool prescreen = true; double eroC0 = 0.06 - 1e-9, eroC1 = 0.42 - 1e-9; bool eroSplit = false; double eroT1 = -0.110 - 1e-9, eroT3 = 0.274 - 1e-9; unsigned long long* dBLx_ = nullptr; FILE* dumpA2 = nullptr;
    bool solve5 = false; const char* planPath = nullptr; const char* tasksFile = nullptr; int fusedBlk = 64, fusedGrid = 0, fusedGpb = 32, fusedTier = 1, fusedSlots = 2, fusedHostThreads = 2; FILE* dumpTasks5 = nullptr;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--catalog") && i + 1 < argc) catPath = argv[++i];
        else if (!strcmp(argv[i], "--min-air") && i + 1 < argc) minAir = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--first-index") && i + 1 < argc) firstIndex = atoll(argv[++i]);
        else if (!strcmp(argv[i], "--count") && i + 1 < argc) count = atoll(argv[++i]);
        else if (!strcmp(argv[i], "--max-height") && i + 1 < argc) maxH = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--cols") && i + 1 < argc) ncols = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--u-lo") && i + 1 < argc) uLo = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--u-hi") && i + 1 < argc) uHi = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--u-batch") && i + 1 < argc) uBatch = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--no-swamp")) swamp = false;
        else if (!strcmp(argv[i], "--invert")) invert = true;
        else if (!strcmp(argv[i], "--solve")) solve = true;
        else if (!strcmp(argv[i], "--solve-only")) { solve = true; solveOnly = true; }
        else if (!strcmp(argv[i], "--solve-nohut")) { solve = true; solveOnly = true; noHut = true; }
        else if (!strcmp(argv[i], "--dump-tasks") && i + 1 < argc) { dumpTasks = fopen(argv[++i], "w"); }
        else if (!strcmp(argv[i], "--dump-hits") && i + 1 < argc) { dumpHits = fopen(argv[++i], "w"); }
        else if (!strcmp(argv[i], "--dump-lists") && i + 1 < argc) { dumpLists = fopen(argv[++i], "w"); }
        else if (!strcmp(argv[i], "--min-lo") && i + 1 < argc) minLo = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--task-batch") && i + 1 < argc) taskBatch = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--solve-one") && i + 3 < argc) { one = true; solve = true; oneX = strtoull(argv[i + 1], 0, 10); oneCx = atoll(argv[i + 2]); oneCz = atoll(argv[i + 3]); i += 3; }
        else if (!strcmp(argv[i], "--spots") && i + 1 < argc) spotsPath = argv[++i];
        else if (!strcmp(argv[i], "--far")) { regX = 2; regZ = 2; }
        else if (!strcmp(argv[i], "--regime") && i + 1 < argc) { const char* rg = argv[++i]; if (strlen(rg) != 2 || rg[0] < '0' || rg[0] > '2' || rg[1] < '0' || rg[1] > '2') { fprintf(stderr, "--regime XZ: two digits 0 (exact), 1 (mid), 2 (far)\n"); return 2; } regX = rg[0] - '0'; regZ = rg[1] - '0'; }
        else if (!strcmp(argv[i], "--no-stage")) noStage = true;
        else if (!strcmp(argv[i], "--no-fast")) noFast = true;
        else if (!strcmp(argv[i], "--no-shared")) noSh = true;
        else if (!strcmp(argv[i], "--no-stage1")) noS1 = true;
        else if (!strcmp(argv[i], "--no-fuse")) noFuse = true;
        else if (!strcmp(argv[i], "--no-pit-stage")) noPitStage = true;
        else if (!strcmp(argv[i], "--solve-old")) solveOld = true;
        else if (!strcmp(argv[i], "--batch") && i + 1 < argc) batchReq = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--no-async")) noAsync = true;
        else if (!strcmp(argv[i], "--odd-tasks")) oddTasks = true;
        else if (!strcmp(argv[i], "--solve3")) solve3 = true;
        else if (!strcmp(argv[i], "--item-cap-mi") && i + 1 < argc) itemCapMi = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--no-prescreen")) prescreen = false;
        else if (!strcmp(argv[i], "--ero-c0") && i + 1 < argc) eroC0 = atof(argv[++i]);
        else if (!strcmp(argv[i], "--ero-split")) eroSplit = true;
        else if (!strcmp(argv[i], "--ero-t1") && i + 1 < argc) eroT1 = atof(argv[++i]) - 1e-9;
        else if (!strcmp(argv[i], "--ero-t3") && i + 1 < argc) eroT3 = atof(argv[++i]) - 1e-9;
        else if (!strcmp(argv[i], "--ero-c1") && i + 1 < argc) eroC1 = atof(argv[++i]);
        else if (!strcmp(argv[i], "--dump-a2") && i + 1 < argc) { dumpA2 = fopen(argv[++i], "w"); if (!dumpA2) { fprintf(stderr, "cannot write the --dump-a2 file\n"); return 2; } }
        else if (!strcmp(argv[i], "--solve4")) { solve3 = true; solve4 = true; }
        else if (!strcmp(argv[i], "--solve5")) { solve = true; solveOnly = true; solve5 = true; }
        else if (!strcmp(argv[i], "--plan") && i + 1 < argc) planPath = argv[++i];
        else if (!strcmp(argv[i], "--fused-blk") && i + 1 < argc) fusedBlk = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--fused-grid") && i + 1 < argc) fusedGrid = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--fused-tier") && i + 1 < argc) fusedTier = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--fused-gpb") && i + 1 < argc) fusedGpb = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--fused-slots") && i + 1 < argc) fusedSlots = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--fused-threads") && i + 1 < argc) fusedHostThreads = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--tasks-file") && i + 1 < argc) tasksFile = argv[++i];
        else if (!strcmp(argv[i], "--quiet")) quiet = true;
        else if (!strcmp(argv[i], "--profile")) profile = true;
        else if (!strcmp(argv[i], "--timing")) timing = true;
        else if (!strcmp(argv[i], "--out") && i + 1 < argc) outPath = argv[++i];
        else { fprintf(stderr, "unknown arg %s\n", argv[i]); return 2; }
    }
    if (!catPath) { fprintf(stderr, "usage: hut_lookup --catalog cat.txt [--min-air N] --first-index i --count n [--max-height -54] [--cols 6] [--no-swamp] [--out f]\n"); return 2; }
    if (uLo < 0 || uHi > 65536 || uLo >= uHi || uBatch < 1 || uBatch > 4096) { fprintf(stderr, "bad u range / batch\n"); return 2; }

    std::vector<unsigned long long> X; std::vector<unsigned> V;
    {
        const size_t pl = strlen(catPath);
        if (pl > 4 && !strcmp(catPath + pl - 4, ".bin")) {
            FILE* f = fopen(catPath, "rb"); if (!f) { fprintf(stderr, "cannot read %s\n", catPath); return 1; }
            struct Rec { unsigned long long X; unsigned short hi, lo; unsigned pad; } r;
            while (fread(&r, sizeof r, 1, f) == 1) {
                if ((int)r.hi < minAir || (int)r.lo < minLo) continue;
                X.push_back(r.X & S48_MASK); V.push_back(((unsigned)r.hi & 0x3FF) | (((unsigned)r.lo & 0x3FF) << 10));
            }
            fclose(f);
        } else {
            FILE* f = fopen(catPath, "r"); if (!f) { fprintf(stderr, "cannot read %s\n", catPath); return 1; }
            char ln[1024];
            while (fgets(ln, sizeof ln, f)) {
                if (ln[0] == '#' || ln[0] == '\n') continue;
                unsigned long long x; double hi, lo;
                if (sscanf(ln, "%llu %lf %lf", &x, &hi, &lo) != 3) continue;
                if ((int)hi < minAir || (int)lo < minLo) continue;
                X.push_back(x & S48_MASK); V.push_back(((unsigned)hi & 0x3FF) | (((unsigned)lo & 0x3FF) << 10));
            }
            fclose(f);
        }
    }
    if (X.empty()) { fprintf(stderr, "empty catalogue (after --min-air %d)\n", minAir); return 1; }
    int bits = 4; while ((1ULL << bits) < 2ULL * X.size()) bits++;
    std::vector<unsigned long long> keys((size_t)1 << bits, 0); std::vector<unsigned> vals((size_t)1 << bits, 0);
    for (size_t i = 0; i < X.size(); i++) {
        unsigned h = (unsigned)((X[i] * 0x9E3779B97F4A7C15ULL) >> (64 - bits));
        const unsigned mask = (1u << bits) - 1;
        const unsigned long long key = X[i] | (1ULL << 48);
        while (keys[h] != 0 && keys[h] != key) h = (h + 1) & mask;
        keys[h] = key; vals[h] = V[i];
    }
    unsigned long long* dKeys; unsigned* dVals;
    CUDA_OK(cudaMalloc(&dKeys, keys.size() * 8)); CUDA_OK(cudaMemcpy(dKeys, keys.data(), keys.size() * 8, cudaMemcpyHostToDevice));
    CUDA_OK(cudaMalloc(&dVals, vals.size() * 4)); CUDA_OK(cudaMemcpy(dVals, vals.data(), vals.size() * 4, cudaMemcpyHostToDevice));
    Table tab; tab.keys = dKeys; tab.vals = dVals; tab.bits = bits;
    if (!quiet) printf("catalogue: %zu layouts (air >= %d), table 2^%d slots\n", X.size(), minAir, bits);

    const unsigned long long STARTS_MATCH_CAP = 1ULL << 20, SURV_CAP = 1ULL << 22;
    unsigned long long *dStartCount, *dMatchCnt, *dSurvCnt, *dSurv; Match *dMatches; DevNoiseCache* dCaches;
    CUDA_OK(cudaMalloc(&dStartCount, 8)); CUDA_OK(cudaMalloc(&dMatchCnt, 8)); CUDA_OK(cudaMalloc(&dMatches, STARTS_MATCH_CAP * sizeof(Match)));
    CUDA_OK(cudaMalloc(&dCaches, (size_t)uBatch * sizeof(DevNoiseCache)));
    CUDA_OK(cudaMalloc(&dSurvCnt, 8)); CUDA_OK(cudaMalloc(&dSurv, SURV_CAP * 8));
    unsigned long long* dLx = nullptr; unsigned* dLv = nullptr;
    if (invert) { CUDA_OK(cudaMalloc(&dLx, X.size() * 8)); CUDA_OK(cudaMemcpy(dLx, X.data(), X.size() * 8, cudaMemcpyHostToDevice)); CUDA_OK(cudaMalloc(&dLv, V.size() * 4)); CUDA_OK(cudaMemcpy(dLv, V.data(), V.size() * 4, cudaMemcpyHostToDevice)); }
    const bool staged = swamp && !profile && !noStage;
    const unsigned long long LIST_CAP = 24ULL << 20; unsigned* dList = nullptr; unsigned char* dUFlag = nullptr; int* dAlive = nullptr; unsigned long long* dListCnt = nullptr;
    if (staged) { CUDA_OK(cudaMalloc(&dList, LIST_CAP * 4)); CUDA_OK(cudaMalloc(&dUFlag, 65536)); CUDA_OK(cudaMalloc(&dAlive, 4096 * 4)); CUDA_OK(cudaMalloc(&dListCnt, 8)); }
    DevDoublePerlin* dEro = nullptr; int* dUIdx = nullptr; const bool fast = staged && !noFast; unsigned *dL1 = nullptr, *dL2 = nullptr; int* dL3 = nullptr; unsigned long long* dN = nullptr;
    if (fast) { CUDA_OK(cudaMalloc(&dEro, 65536ULL * sizeof(DevDoublePerlin))); CUDA_OK(cudaMalloc(&dUIdx, 65536 * 4)); CUDA_OK(cudaMalloc(&dL1, 65536 * 4)); CUDA_OK(cudaMalloc(&dL2, 65536 * 4)); CUDA_OK(cudaMalloc(&dL3, 65536 * 4)); CUDA_OK(cudaMalloc(&dN, 32)); }
    FILE* out = fopen(outPath, "a"); if (!out) { fprintf(stderr, "cannot write %s\n", outPath); return 1; }
    { fseek(out, 0, SEEK_END); if (ftell(out) == 0) fprintf(out, "seed,chunkX,chunkZ,layoutX,airHi,airLo\n"); }

    const int radius = 58593, rzLo = -radius, nRz = 2 * radius + 1;

    const int nStrips = (nRz + STRIP - 1) / STRIP;
    const auto t0 = std::chrono::steady_clock::now();
    auto wall = [&]() { return std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count(); };
    double tSweep = 0, tBuild = 0, tGate = 0, tAliveB = 0; auto lap = [&]() { CUDA_OK(cudaDeviceSynchronize()); return std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count(); };
    unsigned long long totMatches = 0, totSurv = 0, totPairs = 0; unsigned long long* dProf; CUDA_OK(cudaMalloc(&dProf, NPROF * 8)); CUDA_OK(cudaMemset(dProf, 0, NPROF * 8));

    unsigned *dL3u = nullptr; int* dL3i = nullptr; unsigned long long* dN3b = nullptr; double* dApb = nullptr; unsigned* dL2f = nullptr; unsigned long long* dN2f = nullptr;
    TabItem *dTabCave = nullptr, *dTabRestSky = nullptr, *dTabRestHut = nullptr; int nCave = 0, nRestSky = 0, nRestHut = 0;
    if (!noFuse) {
        CUDA_OK(cudaMalloc(&dL3u, 65536 * 4)); CUDA_OK(cudaMalloc(&dL3i, 65536 * 4)); CUDA_OK(cudaMalloc(&dN3b, 8)); CUDA_OK(cudaMalloc(&dApb, 65536ULL * 16));
        CUDA_OK(cudaMalloc(&dL2f, 65536 * 4)); CUDA_OK(cudaMalloc(&dN2f, 8));
        auto up = [&](const std::vector<TabItem>& v, TabItem*& d, int& nn) { nn = (int)v.size(); CUDA_OK(cudaMalloc(&d, v.size() * sizeof(TabItem))); CUDA_OK(cudaMemcpy(d, v.data(), v.size() * sizeof(TabItem), cudaMemcpyHostToDevice)); };
        up(makeTabItems({ S_CE, S_RAR, S_THK, S_L1, S_L2, S_MOD, S_ROU }), dTabCave, nCave);
        up(makeTabItems({ S_LAY, S_CHE, S_AQ }), dTabRestSky, nRestSky);
        up(makeTabItems({ S_ERO, S_TEM, S_CON, S_RID, S_LAY, S_CHE, S_AQ }), dTabRestHut, nRestHut);
    }
    double tFcave = 0, tFg12 = 0, tFrest = 0;
    auto dumpList = [&](long long s48, int stage, const void* dev, unsigned long long n, bool isInt) {
        if (!dumpLists || !n) return;
        std::vector<unsigned> h(n); CUDA_OK(cudaMemcpy(h.data(), dev, n * 4, cudaMemcpyDeviceToHost)); std::sort(h.begin(), h.end());
        for (unsigned x : h) fprintf(dumpLists, "%lld %d %u\n", s48, stage, x);
        (void)isInt;
    };
    auto fusedTail = [&](long long s48, const unsigned* dCand, unsigned long long nCand, bool swampMode) {
        const int nRest = swampMode ? nRestHut : nRestSky; const TabItem* dRest = swampMode ? dTabRestHut : dTabRestSky;
        for (unsigned long long q0 = 0; q0 < nCand; q0 += (unsigned long long)uBatch) {
            const unsigned long long cnt = std::min<unsigned long long>((unsigned long long)uBatch, nCand - q0);
            const unsigned* cand = dCand + q0;
            const double c0 = timing ? lap() : 0;
            CUDA_OK(cudaMemset(dN3b, 0, 8));
            parBuildKernel<<<(unsigned)((cnt * nCave + SH_BLK2 - 1) / SH_BLK2), SH_BLK2>>>(s48, cand, nullptr, nullptr, cnt, dTabCave, nCave, dCaches); CUDA_OK(cudaGetLastError());
            const double c1 = timing ? lap() : 0;
            gate12Kernel<<<(unsigned)((cnt + 63) / 64), 64>>>(dCaches, cand, nullptr, cnt, dMatches, dL3u, dL3i, dN3b); CUDA_OK(cudaGetLastError());
            unsigned long long n3 = 0; CUDA_OK(cudaMemcpy(&n3, dN3b, 8, cudaMemcpyDeviceToHost));
            const double c2 = timing ? lap() : 0;
            dumpList(s48, 3, dL3u, n3, false);
            if (n3) {
                parBuildKernel<<<(unsigned)((n3 * nRest + SH_BLK2 - 1) / SH_BLK2), SH_BLK2>>>(s48, dL3u, dL3i, nullptr, n3, dRest, nRest, dCaches); CUDA_OK(cudaGetLastError());
                finalGate2Kernel<<<(unsigned)((n3 + 63) / 64), 64>>>(dCaches, dL3i, dL3u, nullptr, n3, dMatches, maxH, ncols, dSurvCnt, dSurv, SURV_CAP, swampMode); CUDA_OK(cudaGetLastError());
            }
            if (timing) { const double c3 = lap(); tFcave += c1 - c0; tFg12 += c2 - c1; tFrest += c3 - c2; totN3f += n3; }
        }
    };
    auto emitSurvivors = [&](long long s48, const std::vector<Match>& mm) -> unsigned long long {
        unsigned long long ns = 0, survS = 0; CUDA_OK(cudaMemcpy(&ns, dSurvCnt, 8, cudaMemcpyDeviceToHost));
        if (ns > SURV_CAP) { fprintf(stderr, "*** survivor buffer overflow (%llu)\n", ns); exit(1); }
        if (ns) {
            std::vector<unsigned long long> sv(ns); CUDA_OK(cudaMemcpy(sv.data(), dSurv, ns * 8, cudaMemcpyDeviceToHost));
            for (unsigned long long pr : sv) {
                const Match& m = mm[0];
                const long long ws = (long long)((pr << 48) | (unsigned long long)s48);
                fprintf(out, "%lld,%d,%d,%llu,%u,%u\n", ws, m.cx, m.cz, m.carver, m.val & 0x3FF, (m.val >> 10) & 0x3FF);
                survS++;
            }
        }
        return survS;
    };

    const unsigned long long CAP3 = 1024;
    auto asyncTail = [&](long long s48, const unsigned* dCand, const unsigned long long* dCandCnt, bool swampMode) -> bool {
        const int nRest = swampMode ? nRestHut : nRestSky; const TabItem* dRest = swampMode ? dTabRestHut : dTabRestSky;
        CUDA_OK(cudaMemsetAsync(dN3b, 0, 8));
        parBuildKernel<<<(unsigned)(((unsigned long long)uBatch * nCave + SH_BLK2 - 1) / SH_BLK2), SH_BLK2>>>(s48, dCand, nullptr, dCandCnt, (unsigned long long)uBatch, dTabCave, nCave, dCaches);
        gate12Kernel<<<(unsigned)((uBatch + 63) / 64), 64>>>(dCaches, dCand, dCandCnt, (unsigned long long)uBatch, dMatches, dL3u, dL3i, dN3b);
        parBuildKernel<<<(unsigned)((CAP3 * nRest + SH_BLK2 - 1) / SH_BLK2), SH_BLK2>>>(s48, dL3u, dL3i, dN3b, CAP3, dRest, nRest, dCaches);
        finalGate2Kernel<<<(unsigned)((CAP3 + 63) / 64), 64>>>(dCaches, dL3i, dL3u, dN3b, CAP3, dMatches, maxH, ncols, dSurvCnt, dSurv, SURV_CAP, swampMode);
        CUDA_OK(cudaGetLastError());
        unsigned long long nc = 0, n3 = 0; CUDA_OK(cudaMemcpy(&nc, dCandCnt, 8, cudaMemcpyDeviceToHost)); CUDA_OK(cudaMemcpy(&n3, dN3b, 8, cudaMemcpyDeviceToHost));
        return nc <= (unsigned long long)uBatch && n3 <= CAP3;
    };

    const bool batchCommon = !noFuse && !noAsync && !timing && !dumpLists && ncols == 6 && uLo == 0 && uHi == 65536;
    const bool batchOn = batchReq != 0 && batchCommon && (swamp ? fast : (!profile && !noPitStage));
    const int batchS = batchReq > 0 ? std::min(batchReq, 64) : (swamp ? 16 : 4);
    long long* dBS48 = nullptr; double* dBApb = nullptr; unsigned *dBLx = nullptr, *dBLy = nullptr, *dBL0 = nullptr, *dBL1 = nullptr, *dBL2 = nullptr, *dBL2f = nullptr, *dBL3e = nullptr; int* dBL3i = nullptr; unsigned long long* dBN = nullptr;
    if (batchOn) {
        CUDA_OK(cudaMalloc(&dBS48, 64 * 8)); CUDA_OK(cudaMalloc(&dBApb, (size_t)batchS * 65536 * 16));
        CUDA_OK(cudaMalloc(&dBL1, (size_t)batchS * 65536 * 4)); CUDA_OK(cudaMalloc(&dBL2, (size_t)batchS * 65536 * 4)); CUDA_OK(cudaMalloc(&dBL2f, (size_t)batchS * 65536 * 4));
        CUDA_OK(cudaMalloc(&dBL3e, 65536 * 4)); CUDA_OK(cudaMalloc(&dBL3i, 65536 * 4)); CUDA_OK(cudaMalloc(&dBN, 64));
        CUDA_OK(cudaMalloc(&dBL0, (size_t)batchS * 65536 * 4)); CUDA_OK(cudaMemcpyToSymbol(c_eroC0, &eroC0, 8)); CUDA_OK(cudaMemcpyToSymbol(c_eroC1, &eroC1, 8));
        CUDA_OK(cudaMemcpyToSymbol(c_eroT1, &eroT1, 8)); CUDA_OK(cudaMemcpyToSymbol(c_eroT3, &eroT3, 8)); CUDA_OK(cudaMalloc(&dBLx, (size_t)batchS * 65536 * 4)); CUDA_OK(cudaMalloc(&dBLy, (size_t)batchS * 65536 * 4));
    }
    auto terrainBatch = [&](const std::vector<long long>& s48v, const std::vector<Match>& mv, bool swampMode) -> long long {
        const int S = (int)s48v.size();
        const unsigned long long CAP1 = (unsigned long long)S * 32768, CAP2 = (unsigned long long)S * 8192, CAP3b = swampMode ? 1024 : 4096, CAPA0 = (unsigned long long)S * 32768;
        const int nRest = swampMode ? nRestHut : nRestSky; const TabItem* dRest = swampMode ? dTabRestHut : dTabRestSky;
        CUDA_OK(cudaMemcpy(dBS48, s48v.data(), (size_t)S * 8, cudaMemcpyHostToDevice));
        CUDA_OK(cudaMemcpy(dMatches, mv.data(), (size_t)S * sizeof(Match), cudaMemcpyHostToDevice));
        CUDA_OK(cudaMemsetAsync(dBN, 0, 64)); CUDA_OK(cudaMemsetAsync(dSurvCnt, 0, 8));
        if (swampMode) {
            if (prescreen) {
                if (eroSplit) {
                    stageA0aKernel<<<dim3(65536 / SH_BLK2, S), SH_BLK2>>>(dBS48, dMatches, dBApb, dBLx, dBN + 5);
                    stageA0cKernel<<<(unsigned)(((unsigned long long)S * 65536 + SH_BLK2 - 1) / SH_BLK2), SH_BLK2>>>(dBS48, dMatches, dBApb, dBLx, dBN + 5, (unsigned long long)S * 65536, dBL0, dBN + 4);
                    stageA1aKernel<<<(unsigned)((CAPA0 + SH_BLK2 - 1) / SH_BLK2), SH_BLK2>>>(dBS48, dMatches, dBApb, dBL0, dBN + 4, CAPA0, dBLy, dBN + 6);
                    stageA1cKernel<<<(unsigned)((CAPA0 + SH_BLK2 - 1) / SH_BLK2), SH_BLK2>>>(dBS48, dMatches, dBApb, dBLy, dBN + 6, CAPA0, dBL1, dBN + 0);
                } else {
                stageA0bKernel<<<dim3(65536 / SH_BLK2, S), SH_BLK2>>>(dBS48, dMatches, dBApb, dBL0, dBN + 4);
                stageA1pbKernel<<<(unsigned)((CAPA0 + SH_BLK2 - 1) / SH_BLK2), SH_BLK2>>>(dBS48, dMatches, dBApb, dBL0, dBN + 4, CAPA0, dBL1, dBN + 0);
                }
            } else stageA1bKernel<<<dim3(65536 / SH_BLK2, S), SH_BLK2>>>(dBS48, dMatches, dBApb, dBL1, dBN + 0);
            stageA2bKernel<<<(unsigned)((CAP1 + SH_BLK2 - 1) / SH_BLK2), SH_BLK2>>>(dBS48, dMatches, dBApb, dBL1, dBN + 0, CAP1, dBL2, dBN + 1);
            stageB1bKernel<<<(unsigned)((CAP2 + SH_BLK2 - 1) / SH_BLK2), SH_BLK2>>>(dBS48, dMatches, dBL2, dBN + 1, CAP2, dBL2f, dBN + 2);
        } else {
            stageB1bKernel<<<dim3(65536 / SH_BLK2, S), SH_BLK2>>>(dBS48, dMatches, nullptr, nullptr, 0, dBL2f, dBN + 2);
        }
        parBuildKernel<<<(unsigned)(((unsigned long long)uBatch * nCave + SH_BLK2 - 1) / SH_BLK2), SH_BLK2>>>(0, dBL2f, nullptr, dBN + 2, (unsigned long long)uBatch, dTabCave, nCave, dCaches, dBS48);
        gate12bKernel<<<(unsigned)((uBatch + 63) / 64), 64>>>(dCaches, dBL2f, dBN + 2, (unsigned long long)uBatch, dMatches, dBL3e, dBL3i, dBN + 3);
        parBuildKernel<<<(unsigned)((CAP3b * nRest + SH_BLK2 - 1) / SH_BLK2), SH_BLK2>>>(0, dBL3e, dBL3i, dBN + 3, CAP3b, dRest, nRest, dCaches, dBS48);
        finalGate2bKernel<<<(unsigned)((CAP3b + 63) / 64), 64>>>(dCaches, dBL3i, dBL3e, dBN + 3, CAP3b, dMatches, maxH, ncols, dSurvCnt, dSurv, SURV_CAP, swampMode);
        CUDA_OK(cudaGetLastError());
        unsigned long long nn[7]; CUDA_OK(cudaMemcpy(nn, dBN, 56, cudaMemcpyDeviceToHost));
        if (swampMode && (nn[0] > CAP1 || nn[1] > CAP2 || (prescreen && nn[4] > CAPA0) || (prescreen && eroSplit && nn[6] > CAPA0))) return -1;
        if (dumpA2 && swampMode) { std::vector<unsigned> l2(nn[1]); if (nn[1]) CUDA_OK(cudaMemcpy(l2.data(), dBL2, nn[1] * 4, cudaMemcpyDeviceToHost)); for (unsigned e : l2) fprintf(dumpA2, "%lld %u\n", s48v[e >> 16], e & 0xFFFFu); }
        if (nn[2] > (unsigned long long)uBatch || nn[3] > CAP3b) return -1;
        unsigned long long ns = 0; CUDA_OK(cudaMemcpy(&ns, dSurvCnt, 8, cudaMemcpyDeviceToHost));
        if (ns > SURV_CAP) { fprintf(stderr, "*** survivor buffer overflow (%llu)\n", ns); exit(1); }
        if (ns) {
            std::vector<unsigned long long> sv(ns); CUDA_OK(cudaMemcpy(sv.data(), dSurv, ns * 8, cudaMemcpyDeviceToHost));
            for (unsigned long long pr : sv) {
                const unsigned spot = (unsigned)(pr >> 16), u = (unsigned)(pr & 0xFFFFu); const Match& m = mv[spot];
                const long long ws = (long long)(((unsigned long long)u << 48) | (unsigned long long)s48v[spot]);
                fprintf(out, "%lld,%d,%d,%llu,%u,%u\n", ws, m.cx, m.cz, m.carver, m.val & 0x3FF, (m.val >> 10) & 0x3FF);
            }
        }
        return (long long)ns;
    };

    auto terrain = [&](long long s48, unsigned long long Mn, const std::vector<Match>& mm, long long idxMsg) -> unsigned long long {
        unsigned long long survS = 0;
        const bool useAsync = !noFuse && !noAsync && !timing && !dumpLists;
        if (useAsync && !swamp && !profile && !noPitStage && Mn == 1 && ncols == 6 && uLo == 0 && uHi == 65536) {
            CUDA_OK(cudaMemsetAsync(dN2f, 0, 8)); CUDA_OK(cudaMemsetAsync(dSurvCnt, 0, 8));
            stageB1Kernel<<<65536 / SH_BLK2, SH_BLK2>>>(s48, dMatches, nullptr, 0, dL2f, dN2f, nullptr);
            if (asyncTail(s48, dL2f, dN2f, false)) return emitSurvivors(s48, mm);
            unsigned long long n2f = 0; CUDA_OK(cudaMemcpy(&n2f, dN2f, 8, cudaMemcpyDeviceToHost)); CUDA_OK(cudaMemset(dSurvCnt, 0, 8));
            fusedTail(s48, dL2f, n2f, false);
            return emitSurvivors(s48, mm);
        }
        if (useAsync && fast && Mn == 1 && ncols == 6 && uLo == 0 && uHi == 65536) {
            CUDA_OK(cudaMemsetAsync(dN, 0, 32)); CUDA_OK(cudaMemsetAsync(dSurvCnt, 0, 8));
            stageA1fKernel<<<65536 / SH_BLK2, SH_BLK2>>>(s48, dMatches, dApb, dL1, dN + 0);
            stageA2fKernel<<<65536 / SH_BLK2, SH_BLK2>>>(s48, dMatches, dApb, dL1, dN + 0, 65536ULL, dL2, dN + 1);
            if (asyncTail(s48, dL2, dN + 1, true)) return emitSurvivors(s48, mm);
            unsigned long long n2 = 0; CUDA_OK(cudaMemcpy(&n2, dN + 1, 8, cudaMemcpyDeviceToHost)); CUDA_OK(cudaMemset(dSurvCnt, 0, 8));
            fusedTail(s48, dL2, n2, true);
            return emitSurvivors(s48, mm);
        }
        if (!noFuse && !swamp && !profile && !noPitStage && Mn == 1 && ncols == 6 && uLo == 0 && uHi == 65536) {

            const double b0 = timing ? lap() : 0;
            CUDA_OK(cudaMemset(dN2f, 0, 8)); CUDA_OK(cudaMemset(dSurvCnt, 0, 8));
            stageB1Kernel<<<65536 / SH_BLK2, SH_BLK2>>>(s48, dMatches, nullptr, 0, dL2f, dN2f, nullptr); CUDA_OK(cudaGetLastError());
            unsigned long long n2f = 0; CUDA_OK(cudaMemcpy(&n2f, dN2f, 8, cudaMemcpyDeviceToHost));
            if (timing) { tB1v += lap() - b0; totN2f += n2f; }
            dumpList(s48, 1, dL2f, n2f, false);
            fusedTail(s48, dL2f, n2f, false);
            return emitSurvivors(s48, mm);
        }
        if (!noFuse && fast && Mn == 1 && ncols == 6 && uLo == 0 && uHi == 65536) {

            const double b0 = timing ? lap() : 0;
            CUDA_OK(cudaMemset(dN, 0, 32)); CUDA_OK(cudaMemset(dSurvCnt, 0, 8));
            stageA1fKernel<<<65536 / SH_BLK2, SH_BLK2>>>(s48, dMatches, dApb, dL1, dN + 0); CUDA_OK(cudaGetLastError());
            unsigned long long n1 = 0; CUDA_OK(cudaMemcpy(&n1, dN + 0, 8, cudaMemcpyDeviceToHost));
            const double a1e = timing ? lap() : 0;
            if (n1) { stageA2fKernel<<<(unsigned)((n1 + SH_BLK2 - 1) / SH_BLK2), SH_BLK2>>>(s48, dMatches, dApb, dL1, nullptr, n1, dL2, dN + 1); CUDA_OK(cudaGetLastError()); }
            unsigned long long n2 = 0; CUDA_OK(cudaMemcpy(&n2, dN + 1, 8, cudaMemcpyDeviceToHost));
            if (timing) { const double a2e = lap(); tHA1 += a1e - b0; tHA2 += a2e - a1e; totH1 += n1; totH2 += n2; }
            dumpList(s48, 1, dL1, n1, false); dumpList(s48, 2, dL2, n2, false);
            fusedTail(s48, dL2, n2, true);
            return emitSurvivors(s48, mm);
        }
        if (!swamp && !profile && !noPitStage && Mn == 1 && ncols == 6 && uLo == 0 && uHi == 65536) {

            static int *dIdent = nullptr, *dL3p = nullptr; static unsigned long long* dNp = nullptr;
            if (!dIdent) {
                std::vector<int> id(65536); for (int i = 0; i < 65536; i++) id[i] = i;
                CUDA_OK(cudaMalloc(&dIdent, 65536 * 4)); CUDA_OK(cudaMemcpy(dIdent, id.data(), 65536 * 4, cudaMemcpyHostToDevice));
                CUDA_OK(cudaMalloc(&dL3p, 65536 * 4)); CUDA_OK(cudaMalloc(&dNp, 8));
            }
            const double b0 = timing ? lap() : 0;
            CUDA_OK(cudaMemset(dNp, 0, 8)); CUDA_OK(cudaMemset(dSurvCnt, 0, 8));
            const int SB = 4096;
            if (noFuse) {
                for (int u0 = 0; u0 < 65536; u0 += SB) { stageBKernel<<<SB / SH_BLK, SH_BLK>>>(s48, dMatches, dCaches, (const unsigned*)(dIdent + u0), SB, dL3p, dNp); CUDA_OK(cudaGetLastError()); }
            } else {
                static unsigned* dL2f = nullptr; static unsigned long long* dN2f = nullptr;
                if (!dL2f) { CUDA_OK(cudaMalloc(&dL2f, 65536 * 4)); CUDA_OK(cudaMalloc(&dN2f, 8)); }
                CUDA_OK(cudaMemset(dN2f, 0, 8));
                stageB1Kernel<<<65536 / SH_BLK2, SH_BLK2>>>(s48, dMatches, nullptr, 0, dL2f, dN2f, nullptr); CUDA_OK(cudaGetLastError());
                unsigned long long n2f = 0; CUDA_OK(cudaMemcpy(&n2f, dN2f, 8, cudaMemcpyDeviceToHost)); if (timing) { tB1v += lap() - b0; totN2f += n2f; }
                for (unsigned long long q0 = 0; q0 < n2f; q0 += SB) {
                    const unsigned long long cnt = std::min<unsigned long long>(SB, n2f - q0);
                    stageBKernel<<<(unsigned)((cnt + SH_BLK - 1) / SH_BLK), SH_BLK>>>(s48, dMatches, dCaches, dL2f + q0, cnt, dL3p, dNp); CUDA_OK(cudaGetLastError());
                }
            }
            unsigned long long n3 = 0; CUDA_OK(cudaMemcpy(&n3, dNp, 8, cudaMemcpyDeviceToHost));
            if (timing) { tBuild += lap() - b0; totN3f += n3; }
            if (n3 <= (unsigned long long)uBatch) {
                const double g0 = timing ? lap() : 0;
                if (n3) {
                    buildAliveSkyKernel<<<(unsigned)((n3 + BLK - 1) / BLK), BLK>>>(s48, dL3p, (int)n3, dCaches); CUDA_OK(cudaGetLastError());
                    finalGate1Kernel<<<(unsigned)((n3 + 63) / 64), 64>>>(dCaches, dL3p, (int)n3, dMatches, maxH, ncols, dSurvCnt, dSurv, SURV_CAP, false); CUDA_OK(cudaGetLastError());
                }
                if (timing) tGate += lap() - g0;
                unsigned long long ns = 0; CUDA_OK(cudaMemcpy(&ns, dSurvCnt, 8, cudaMemcpyDeviceToHost));
                if (ns > SURV_CAP) { fprintf(stderr, "*** survivor buffer overflow (%llu)\n", ns); exit(1); }
                if (ns) {
                    std::vector<unsigned long long> sv(ns); CUDA_OK(cudaMemcpy(sv.data(), dSurv, ns * 8, cudaMemcpyDeviceToHost));
                    for (unsigned long long pr : sv) {
                        const Match& m = mm[0];
                        const long long ws = (long long)((pr << 48) | (unsigned long long)s48);
                        fprintf(out, "%lld,%d,%d,%llu,%u,%u\n", ws, m.cx, m.cz, m.carver, m.val & 0x3FF, (m.val >> 10) & 0x3FF);
                        survS++;
                    }
                }
                return survS;
            }

        }
        if (fast && !noS1 && !noSh && Mn == 1 && ncols == 6 && uLo == 0 && uHi == 65536) {
            const double b0 = timing ? lap() : 0;
            CUDA_OK(cudaMemset(dN, 0, 32)); CUDA_OK(cudaMemset(dSurvCnt, 0, 8));
            stageA1Kernel<<<65536 / SH_BLK, SH_BLK>>>(s48, dMatches, dEro, dL1, dN + 0); CUDA_OK(cudaGetLastError());
            unsigned long long n1 = 0; CUDA_OK(cudaMemcpy(&n1, dN + 0, 8, cudaMemcpyDeviceToHost));
            const double a1e = timing ? lap() : 0;
            if (n1) { stageA2Kernel<<<(unsigned)((n1 + SH_BLK - 1) / SH_BLK), SH_BLK>>>(s48, dMatches, dEro, dL1, n1, dL2, dN + 1); CUDA_OK(cudaGetLastError()); }
            unsigned long long n2 = 0; CUDA_OK(cudaMemcpy(&n2, dN + 1, 8, cudaMemcpyDeviceToHost));
            if (timing) { tBuild += lap() - b0; tHA1 += a1e - b0; tHA2 += lap() - a1e; totH1 += n1; totH2 += n2; }
            dumpList(s48, 1, dL1, n1, false); dumpList(s48, 2, dL2, n2, false);
            if (n2 <= (unsigned long long)uBatch) {
                const double r0 = timing ? lap() : 0;
                if (n2) { stageBKernel<<<(unsigned)((n2 + SH_BLK - 1) / SH_BLK), SH_BLK>>>(s48, dMatches, dCaches, dL2, n2, dL3, dN + 2); CUDA_OK(cudaGetLastError()); }
                unsigned long long n3 = 0; CUDA_OK(cudaMemcpy(&n3, dN + 2, 8, cudaMemcpyDeviceToHost));
                if (timing) { const double dt = lap() - r0; tBuild += dt; tAliveB += dt; tHB += dt; totN3f += n3; }
                dumpList(s48, 3, dL3, n3, true);
                if (n3) {
                    const double g0 = timing ? lap() : 0;
                    buildAliveKernel<<<(unsigned)((n3 + BLK - 1) / BLK), BLK>>>(s48, dL3, (int)n3, dCaches); CUDA_OK(cudaGetLastError());
                    finalGate1Kernel<<<(unsigned)((n3 + 63) / 64), 64>>>(dCaches, dL3, (int)n3, dMatches, maxH, ncols, dSurvCnt, dSurv, SURV_CAP); CUDA_OK(cudaGetLastError());
                    if (timing) { tGate += lap() - g0; tHC += lap() - g0; }
                }
                unsigned long long ns = 0; CUDA_OK(cudaMemcpy(&ns, dSurvCnt, 8, cudaMemcpyDeviceToHost));
                if (ns > SURV_CAP) { fprintf(stderr, "*** survivor buffer overflow (%llu)\n", ns); exit(1); }
                if (ns) {
                    std::vector<unsigned long long> sv(ns); CUDA_OK(cudaMemcpy(sv.data(), dSurv, ns * 8, cudaMemcpyDeviceToHost));
                    for (unsigned long long pr : sv) {
                        const Match& m = mm[0];
                        const long long ws = (long long)((pr << 48) | (unsigned long long)s48);
                        fprintf(out, "%lld,%d,%d,%llu,%u,%u\n", ws, m.cx, m.cz, m.carver, m.val & 0x3FF, (m.val >> 10) & 0x3FF);
                        survS++;
                    }
                }
                return survS;
            }

        }
        if (fast && Mn && Mn <= 16 && uLo == 0 && uHi == 65536) {
            const double b0 = timing ? lap() : 0;
            const unsigned long long pairs = 65536ULL * Mn;
            if (noSh) buildEroKernel<<<(65536 + BLK - 1) / BLK, BLK>>>(s48, 65536, dEro); else buildEroShKernel<<<65536 / SH_BLK, SH_BLK>>>(s48, 65536, dEro);
            CUDA_OK(cudaGetLastError());
            CUDA_OK(cudaMemset(dListCnt, 0, 8)); CUDA_OK(cudaMemset(dUFlag, 0, 65536)); CUDA_OK(cudaMemset(dSurvCnt, 0, 8));
            eroGateKernel<<<(unsigned)((pairs + 255) / 256), 256>>>(dEro, dMatches, Mn, pairs, dList, dListCnt, LIST_CAP, dUFlag); CUDA_OK(cudaGetLastError());
            unsigned long long nl = 0; CUDA_OK(cudaMemcpy(&nl, dListCnt, 8, cudaMemcpyDeviceToHost));
            static unsigned char uf[65536]; CUDA_OK(cudaMemcpy(uf, dUFlag, 65536, cudaMemcpyDeviceToHost));
            static int alive[65536], uidx[65536]; int na = 0;
            for (int i = 0; i < 65536; i++) { if (uf[i]) { uidx[i] = na; if (na < 65536) alive[na] = i; na++; } else uidx[i] = -1; }
            if (nl <= LIST_CAP && na <= uBatch) {
                if (timing) tBuild += lap() - b0;
                if (nl) {
                    const double r0 = timing ? lap() : 0;
                    CUDA_OK(cudaMemcpy(dAlive, alive, na * 4, cudaMemcpyHostToDevice)); CUDA_OK(cudaMemcpy(dUIdx, uidx, 65536 * 4, cudaMemcpyHostToDevice));
                    buildAliveKernel<<<(na + BLK - 1) / BLK, BLK>>>(s48, dAlive, na, dCaches); CUDA_OK(cudaGetLastError());
                    if (timing) { const double dt = lap() - r0; tBuild += dt; tAliveB += dt; }
                    const double g0 = timing ? lap() : 0;
                    gateListKernel2<<<(unsigned)((nl + 255) / 256), 256>>>(dCaches, dUIdx, dMatches, Mn, dList, nl, maxH, ncols, dSurvCnt, dSurv, SURV_CAP); CUDA_OK(cudaGetLastError());
                    if (timing) tGate += lap() - g0;
                }
                unsigned long long ns = 0; CUDA_OK(cudaMemcpy(&ns, dSurvCnt, 8, cudaMemcpyDeviceToHost));
                if (ns > SURV_CAP) { fprintf(stderr, "*** survivor buffer overflow (%llu)\n", ns); exit(1); }
                if (ns) {
                    std::vector<unsigned long long> sv(ns); CUDA_OK(cudaMemcpy(sv.data(), dSurv, ns * 8, cudaMemcpyDeviceToHost));
                    for (unsigned long long pr : sv) {
                        const unsigned long long ul = pr / Mn, mi = pr % Mn; const Match& m = mm[mi];
                        const long long ws = (long long)((ul << 48) | (unsigned long long)s48);
                        fprintf(out, "%lld,%d,%d,%llu,%u,%u\n", ws, m.cx, m.cz, m.carver, m.val & 0x3FF, (m.val >> 10) & 0x3FF);
                        survS++;
                    }
                }
                return survS;
            }
            if (timing) tBuild += lap() - b0;
        }
        const int batch = (int)std::max<unsigned long long>(1, std::min<unsigned long long>(uBatch, (24ULL << 20) / (Mn ? Mn : 1)));
        for (int u0 = uLo; u0 < uHi && Mn; u0 += batch) {
            const int nb = std::min(batch, uHi - u0);
            const double b0 = timing ? lap() : 0;
            const unsigned long long pairs = (unsigned long long)nb * Mn;
            if (staged) {

                buildErosionKernel<<<(nb + BLK - 1) / BLK, BLK>>>(s48, u0, nb, dCaches); CUDA_OK(cudaGetLastError());
                CUDA_OK(cudaMemset(dListCnt, 0, 8)); CUDA_OK(cudaMemset(dUFlag, 0, 4096)); CUDA_OK(cudaMemset(dSurvCnt, 0, 8));
                erosionGateKernel<<<(unsigned)((pairs + 255) / 256), 256>>>(dCaches, dMatches, Mn, pairs, dList, dListCnt, LIST_CAP, dUFlag); CUDA_OK(cudaGetLastError());
                unsigned long long nl = 0; CUDA_OK(cudaMemcpy(&nl, dListCnt, 8, cudaMemcpyDeviceToHost));
                if (nl > LIST_CAP) { fprintf(stderr, "*** erosion survivor list overflow (%llu)\n", nl); exit(1); }
                unsigned char uf[4096]; CUDA_OK(cudaMemcpy(uf, dUFlag, 4096, cudaMemcpyDeviceToHost));
                int alive[4096], na = 0; for (int i = 0; i < nb; i++) if (uf[i]) alive[na++] = i;
                if (timing) tBuild += lap() - b0;
                if (nl) {
                    const double r0 = timing ? lap() : 0;
                    CUDA_OK(cudaMemcpy(dAlive, alive, na * 4, cudaMemcpyHostToDevice));
                    buildRestKernel<<<(na + BLK - 1) / BLK, BLK>>>(s48, u0, dAlive, na, dCaches); CUDA_OK(cudaGetLastError());
                    if (timing) tBuild += lap() - r0;
                    const double g0 = timing ? lap() : 0;
                    gateListKernel<<<(unsigned)((nl + 255) / 256), 256>>>(dCaches, dMatches, Mn, dList, nl, maxH, ncols, dSurvCnt, dSurv, SURV_CAP); CUDA_OK(cudaGetLastError());
                    if (timing) tGate += lap() - g0;
                }
            } else {
            buildManyKernel<<<(nb + BLK - 1) / BLK, BLK>>>(s48, u0, nb, dCaches); CUDA_OK(cudaGetLastError());
            if (timing) tBuild += lap() - b0;
            if (profile) { profKernel<<<(unsigned)((pairs + 255) / 256), 256>>>(dCaches, dMatches, Mn, pairs, maxH, dProf); CUDA_OK(cudaGetLastError()); totPairs += pairs; }
            CUDA_OK(cudaMemset(dSurvCnt, 0, 8));
            const double g0 = timing ? lap() : 0;
            gateKernel<<<(unsigned)((pairs + 255) / 256), 256>>>(dCaches, dMatches, Mn, pairs, maxH, ncols, swamp ? 1 : 0, dSurvCnt, dSurv, SURV_CAP); CUDA_OK(cudaGetLastError());
            if (timing) tGate += lap() - g0;
            }
            unsigned long long ns = 0; CUDA_OK(cudaMemcpy(&ns, dSurvCnt, 8, cudaMemcpyDeviceToHost));
            if (ns > SURV_CAP) { fprintf(stderr, "*** survivor buffer overflow (%llu > %llu) at index %lld u0 %d: lower --u-batch\n", ns, SURV_CAP, idxMsg, u0); exit(1); }
            if (ns) {
                std::vector<unsigned long long> sv(ns); CUDA_OK(cudaMemcpy(sv.data(), dSurv, ns * 8, cudaMemcpyDeviceToHost));
                for (unsigned long long pr : sv) {
                    const unsigned long long ul = pr / Mn, mi = pr % Mn; const Match& m = mm[mi];
                    const long long ws = (long long)(((unsigned long long)(unsigned)(u0 + (int)ul) << 48) | (unsigned long long)s48);
                    fprintf(out, "%lld,%d,%d,%llu,%u,%u\n", ws, m.cx, m.cz, m.carver, m.val & 0x3FF, (m.val >> 10) & 0x3FF);
                    survS++;
                }
            }
        }
        return survS;
    };

    auto terrainMany = [&](const std::vector<long long>& sv, const std::vector<Match>& mv) -> unsigned long long {
        unsigned long long tot = 0;
        const size_t G = batchOn ? (size_t)batchS : 1;
        for (size_t i = 0; i < sv.size(); i += G) {
            const size_t j = std::min(sv.size(), i + G);
            if (batchOn) {
                const std::vector<long long> gs(sv.begin() + i, sv.begin() + j); const std::vector<Match> gm(mv.begin() + i, mv.begin() + j);
                const long long r = terrainBatch(gs, gm, swamp);
                if (r >= 0) { tot += (unsigned long long)r; continue; }
            }
            for (size_t k = i; k < j; k++) {
                CUDA_OK(cudaMemcpy(dMatches, &mv[k], sizeof(Match), cudaMemcpyHostToDevice));
                const std::vector<Match> mm(1, mv[k]);
                tot += terrain(sv[k], 1, mm, sv[k]);
            }
        }
        return tot;
    };
    if (spotsPath) {

        FILE* sf = fopen(spotsPath, "r"); if (!sf) { fprintf(stderr, "cannot read %s\n", spotsPath); return 1; }
        char ln[512]; long long nSpots = 0, nBad = 0; unsigned long long totS = 0; double tT = 0;
        std::vector<long long> spS; std::vector<Match> spM;
        while (fgets(ln, sizeof ln, sf)) {
            long long S; int cx, cz;
            if (ln[0] == '#' || sscanf(ln, "%lld %d %d", &S, &cx, &cz) != 3) continue;
            const unsigned long long S48 = (unsigned long long)S & S48_MASK;
            const RoomCtx hc = makeCtx((int64_t)S48);
            Match m; m.cx = cx; m.cz = cz; m.val = 0;
            m.carver = ((unsigned long long)((long long)cx * hc.a) ^ (unsigned long long)((long long)cz * hc.b) ^ S48) & S48_MASK;
            if (!rcIsStartSeed(hc, (int64_t)m.carver)) { nBad++; fprintf(stderr, "*** not a mineshaft start: S=%llu chunk (%d,%d)\n", S48, cx, cz); continue; }
            spS.push_back((long long)S48); spM.push_back(m); nSpots++;
        }
        { const double q0 = lap(); totS = terrainMany(spS, spM); tT = lap() - q0; }
        if (!quiet && totS) fprintf(stderr, "%llu top-bit variants pass the gate over %lld spots\n", totS, nSpots);
        fclose(sf); fclose(out);
        if (profile) {
            unsigned long long h[NPROF]; CUDA_OK(cudaMemcpy(h, dProf, NPROF * 8, cudaMemcpyDeviceToHost));
            const char* nm[] = { "pairs", "erosion>=.55", "temperature", "ridge", "continentalness", "Entrance(y50)<0", "Entrance(y60)<0", "E2&Cheese(maxH)", "ladder y<=0", "ladder y10..40", "aquifer flood", "ALL (marginals AND)", "climate AND", "Entrance50&60", "column gates AND" };
            printf("gate profile over %llu (u,spot) pairs, first footprint column, each gate evaluated independently:\n", totPairs);
            for (int i = 0; i < 15; i++) printf("  %-22s pass %12llu  %.3e\n", nm[i], h[i], totPairs ? (double)h[i] / (double)totPairs : 0.0);
        }
        if (timing) printf("terrain stage inside: B1 fused first test %.3f s, B1+B2 (all twelve tests on survivors) %.3f s, survivors full caches+gate %.3f s | per spot: %.0f variants pass B1, %.0f pass B2\n", tB1v, tBuild, tGate, (double)totN2f / (nSpots ? nSpots : 1), (double)totN3f / (nSpots ? nSpots : 1));
        if (dumpLists) fclose(dumpLists);
        if (timing && (tFcave > 0 || tFg12 > 0)) printf("fused tail (s): cave tables %.3f, 12 exact tests %.3f, remaining tables + final gate %.3f | per spot: %.1f variants pass the 12 tests\n", tFcave, tFg12, tFrest, (double)totN3f / (nSpots ? nSpots : 1));
        if (timing && tHA1 > 0) printf("hut path inside (s): erosion A1 %.3f, A2 %.3f, cave tests B %.3f, alive caches + final gate C %.3f | per spot: %.0f pass A1, %.0f pass A2, %.1f pass B\n", tHA1, tHA2, tHB, tHC, (double)totH1 / (nSpots ? nSpots : 1), (double)totH2 / (nSpots ? nSpots : 1), (double)totN3f / (nSpots ? nSpots : 1));
        printf("spots: %lld spots (%lld not a start), %llu terrain survivors -> %s; terrain %.3f s (%.1f ms per spot)\n", nSpots, nBad, totS, outPath, tT, nSpots ? 1e3 * tT / nSpots : 0.0);
        return nBad ? 1 : 0;
    }
    if (solve5) {

        struct Bundle { unsigned long long X; int rgx, rgz, tier; };
        std::vector<Bundle> bundles;
        if (planPath) {
            FILE* pf = fopen(planPath, "r"); if (!pf) { fprintf(stderr, "cannot read plan %s\n", planPath); return 1; }
            char ln[1024]; while (fgets(ln, sizeof ln, pf)) {
                if (ln[0] == '#' || ln[0] == '\n') continue; unsigned long long x; char rg[8], cls[16] = "odd";
                if (sscanf(ln, "%llu %7s %15s", &x, rg, cls) < 2 || strlen(rg) != 2 || rg[0] < '0' || rg[0] > '2' || rg[1] < '0' || rg[1] > '2') { fprintf(stderr, "bad plan line: %s", ln); return 2; }
                const int tier = !strcmp(cls, "odd") ? 1 : (!strcmp(cls, "u2") ? 2 : (!strcmp(cls, "eq") ? 3 : 0)); if (!tier) { fprintf(stderr, "plan class '%s' unknown (odd | u2 | eq)\n", cls); return 2; }
                bundles.push_back({ x & S48_MASK, rg[0] - '0', rg[1] - '0', tier });
            }
            fclose(pf);
        } else {
            if (regX < 0 || regZ < 0) { fprintf(stderr, "--solve5 needs --regime XZ (or --plan)\n"); return 2; }
            for (size_t i = 0; i < X.size(); i++) bundles.push_back({ X[i], regX, regZ, fusedTier });
        }
        if (bundles.empty()) { fprintf(stderr, "no bundles\n"); return 2; }
        if (firstIndex < 0 || count < 1 || (firstIndex + count) > ((long long)bundles.size() << 20)) { fprintf(stderr, "group range %lld..%lld outside the %zu bundles x 2^20 groups\n", firstIndex, firstIndex + count, bundles.size()); return 2; }
        if (fusedBlk != 64 && fusedBlk != 128 && fusedBlk != 256 && fusedBlk != 512) { fprintf(stderr, "--fused-blk must be 64, 128, 256 or 512\n"); return 2; }
        if (fusedGpb < 1 || fusedGpb > 4096 || fusedSlots < 1 || fusedSlots > 8 || fusedHostThreads < 1 || fusedHostThreads > 32) { fprintf(stderr, "--fused-gpb 1..4096, --fused-slots 1..8, --fused-threads 1..32\n"); return 2; }
        hutPruneListUpload(); fusedNibTableUpload(); fusedMaskTableUpload();
        const int GPB = fusedGpb, NS = fusedSlots; int sms = 0, devId = 0; CUDA_OK(cudaGetDevice(&devId)); CUDA_OK(cudaDeviceGetAttribute(&sms, cudaDevAttrMultiProcessorCount, devId));
        unsigned char* dJo[4][2]; int nJt[4] = { 0, 0, 0, 0 };
        for (int t = 1; t <= 3; t++) { unsigned char jx[288], jz[288]; nJt[t] = fusedOffsetList(t, jx, jz); CUDA_OK(cudaMalloc(&dJo[t][0], 288)); CUDA_OK(cudaMalloc(&dJo[t][1], 288)); CUDA_OK(cudaMemcpy(dJo[t][0], jx, 288, cudaMemcpyHostToDevice)); CUDA_OK(cudaMemcpy(dJo[t][1], jz, 288, cudaMemcpyHostToDevice)); }
        unsigned* dC20; unsigned long long poolCap = (unsigned long long)GPB * 192ULL * 1100ULL; unsigned* dPool; unsigned long long *dPoolCnt, *dStarts; unsigned* dCounts; const unsigned long long HIT_CAP = 1ULL << 16; const unsigned HCOPY = 8192;
        CUDA_OK(cudaMalloc(&dC20, (size_t)GPB * 4)); CUDA_OK(cudaMalloc(&dPool, poolCap * 4)); CUDA_OK(cudaMalloc(&dPoolCnt, 8)); CUDA_OK(cudaMalloc(&dStarts, (size_t)GPB * 960 * 8)); CUDA_OK(cudaMalloc(&dCounts, (size_t)GPB * 960 * 4));
        const size_t regCap = 8192;
        struct Slot { cudaStream_t st; cudaEvent_t e0, e1; FusedRegion* dReg; SolveHit* dHits; unsigned long long* dHitCnt; FusedRegion* hReg; SolveHit* hHits; unsigned long long* hCnt; bool busy = false; long long bundle = 0; unsigned long long nTasks = 0; };
        std::vector<Slot> slots(NS);
        for (auto& s : slots) { CUDA_OK(cudaStreamCreateWithFlags(&s.st, cudaStreamNonBlocking)); cudaEventCreate(&s.e0); cudaEventCreate(&s.e1); CUDA_OK(cudaMalloc(&s.dReg, regCap * sizeof(FusedRegion))); CUDA_OK(cudaMalloc(&s.dHits, HIT_CAP * sizeof(SolveHit))); CUDA_OK(cudaMalloc(&s.dHitCnt, 8));
            CUDA_OK(cudaMallocHost(&s.hReg, regCap * sizeof(FusedRegion))); CUDA_OK(cudaMallocHost(&s.hHits, HIT_CAP * sizeof(SolveHit))); CUDA_OK(cudaMallocHost(&s.hCnt, 8)); }
        cudaEvent_t evB0, evB1; cudaEventCreate(&evB0); cudaEventCreate(&evB1);

        struct Job { std::vector<SolveHit> hits; long long bundle; };
        std::mutex qm, fm; std::condition_variable qcv, qdone; std::deque<Job> q; int inFlight = 0; bool stopW = false;
        std::atomic<unsigned long long> totHits(0), totBad(0); std::vector<std::atomic<unsigned long long>> bHits(bundles.size()); for (auto& a : bHits) a = 0;
        auto worker = [&]() {
            for (;;) {
                Job job; { std::unique_lock<std::mutex> lk(qm); qcv.wait(lk, [&] { return stopW || !q.empty(); }); if (q.empty()) return; job = std::move(q.front()); q.pop_front(); }
                std::sort(job.hits.begin(), job.hits.end(), [](const SolveHit& a, const SolveHit& b2) { return a.S != b2.S ? a.S < b2.S : (a.cx != b2.cx ? a.cx < b2.cx : a.cz < b2.cz); });
                job.hits.erase(std::unique(job.hits.begin(), job.hits.end(), [](const SolveHit& a, const SolveHit& b2) { return a.S == b2.S && a.cx == b2.cx && a.cz == b2.cz; }), job.hits.end());
                std::string outb; char tmp[160];
                for (const SolveHit& h : job.hits) {
                    const RoomCtx hc = makeCtx((int64_t)h.S);
                    const unsigned long long Xc = ((unsigned long long)((long long)h.cx * hc.a) ^ (unsigned long long)((long long)h.cz * hc.b) ^ h.S) & S48_MASK;
                    int32_t ex, ez; rcHutExact(rcRowBase((int64_t)h.S, h.cx >> 5, h.cz >> 5), h.cx >> 5, h.cz >> 5, &ex, &ez);
                    if (!(Xc == h.X && ex == h.cx && ez == h.cz && rcIsStartSeed(hc, (int64_t)h.X))) { totBad++; fprintf(stderr, "*** HOST CHECK FAILED: S=%llu X=%llu chunk (%d,%d)\n", h.S, h.X, h.cx, h.cz); continue; }
                    totHits++; bHits[job.bundle]++; if (dumpHits) { snprintf(tmp, sizeof tmp, "%llu,%d,%d,%llu,1\n", h.S, h.cx, h.cz, h.X); outb += tmp; }
                }
                if (dumpHits && !outb.empty()) { std::lock_guard<std::mutex> lk(fm); fwrite(outb.data(), 1, outb.size(), dumpHits); }
                { std::lock_guard<std::mutex> lk(qm); inFlight--; } qdone.notify_all();
            }
        };
        std::vector<std::thread> pool; for (int i = 0; i < fusedHostThreads; i++) pool.emplace_back(worker);
        auto submit = [&](std::vector<SolveHit>&& hv, long long b) { { std::lock_guard<std::mutex> lk(qm); q.push_back(Job{ std::move(hv), b }); inFlight++; } qcv.notify_one(); };
        double tBuildL = 0, tChain = 0, tHost = 0, tWait = 0; unsigned long long totTasks = 0, totGroups = 0; int poolRegrow = 0, hitOverflow = 0;
        struct BStat { long long groups = 0; unsigned long long tasks = 0; }; std::vector<BStat> bst(bundles.size());
        auto harvest = [&](Slot& s) {
            if (!s.busy) return; const double w0 = wall(); CUDA_OK(cudaEventSynchronize(s.e1)); tWait += wall() - w0;
            { float ms; cudaEventElapsedTime(&ms, s.e0, s.e1); tChain += ms * 1e-3; }
            unsigned long long nh = *s.hCnt; if (nh > HIT_CAP) { fprintf(stderr, "*** solve hit buffer overflow (%llu)\n", nh); exit(1); }
            std::vector<SolveHit> hv; if (nh) { hv.assign(s.hHits, s.hHits + std::min<unsigned long long>(nh, HCOPY)); if (nh > HCOPY) { hv.resize(nh); CUDA_OK(cudaMemcpy(hv.data(), s.dHits, nh * sizeof(SolveHit), cudaMemcpyDeviceToHost)); hitOverflow++; } }
            if (nh) submit(std::move(hv), s.bundle); s.busy = false;
        };
        auto drainAll = [&]() { for (auto& s : slots) harvest(s); };
        long long gi = firstIndex; const long long gEnd = firstIndex + count; int nextSlot = 0;
        std::vector<ts::Region> regs;
        while (gi < gEnd) {
            const long long b = gi >> 20; const Bundle& B = bundles[b]; const ts::Axis ax = ts::axisOf(B.rgx), az = ts::axisOf(B.rgz);
            const int tier = B.tier, mLo = tier == 2 ? 1 : 0, mHi = tier == 1 ? 0 : 4, nLid = 192 * (mHi - mLo + 1), nJ = nJt[tier];
            const long long batchEnd = std::min<long long>(std::min<long long>(gEnd, (b + 1) << 20), gi + GPB); const int nb = (int)(batchEnd - gi);
            drainAll();
            std::vector<unsigned> c20(nb); for (int k = 0; k < nb; k++) c20[k] = ts::permGroup((unsigned)((gi + k) & 0xFFFFF));
            CUDA_OK(cudaMemcpy(dC20, c20.data(), (size_t)nb * 4, cudaMemcpyHostToDevice));
            for (;;) {
                CUDA_OK(cudaMemset(dPoolCnt, 0, 8)); cudaEventRecord(evB0);
                fusedBuildKernel<<<(unsigned)(nb * nLid), 256>>>(dC20, B.X, mLo, mHi, dPool, poolCap, dPoolCnt, dStarts, dCounts); CUDA_OK(cudaGetLastError()); cudaEventRecord(evB1); CUDA_OK(cudaEventSynchronize(evB1));
                unsigned long long used = 0; CUDA_OK(cudaMemcpy(&used, dPoolCnt, 8, cudaMemcpyDeviceToHost)); if (used <= poolCap) break;
                poolCap = used + used / 8; CUDA_OK(cudaFree(dPool)); CUDA_OK(cudaMalloc(&dPool, poolCap * 4)); poolRegrow++;
            }
            { float ms; cudaEventElapsedTime(&ms, evB0, evB1); tBuildL += ms * 1e-3; }
            for (int k = 0; k < nb; k++) {
                Slot& s = slots[nextSlot]; nextSlot = (nextSlot + 1) % NS; harvest(s);
                const double h0 = wall(); ts::regionsOfGroup(c20[k], ax, az, regs); tHost += wall() - h0;
                if (regs.size() > regCap) { fprintf(stderr, "*** a group has %zu regions (> %zu)\n", regs.size(), regCap); return 1; }
                for (size_t i = 0; i < regs.size(); i++) { s.hReg[i].rx = regs[i].rx; s.hReg[i].rz = regs[i].rz; s.hReg[i].Creg = regs[i].Creg; }
                if (dumpTasks) { unsigned char jx[288], jz[288]; const int nj = fusedOffsetList(tier, jx, jz); for (int j = 0; j < nj; j++) for (auto& R : regs) fprintf(dumpTasks, "%llu %d %d\n", B.X, 32 * R.rx + jx[j], 32 * R.rz + jz[j]); }
                const unsigned nReg = (unsigned)regs.size(), grid = fusedGrid <= 0 ? nReg * (unsigned)nJ : (unsigned)(sms * fusedGrid);
                const unsigned* poolG = dPool; const unsigned long long* stG = dStarts + (size_t)k * nLid; const unsigned* cnG = dCounts + (size_t)k * nLid;
                cudaEventRecord(s.e0, s.st);
                CUDA_OK(cudaMemcpyAsync(s.dReg, s.hReg, nReg * sizeof(FusedRegion), cudaMemcpyHostToDevice, s.st)); CUDA_OK(cudaMemsetAsync(s.dHitCnt, 0, 8, s.st));
#define FUSED_LAUNCH(KERN, BS) KERN<BS><<<grid, BS, 0, s.st>>>(s.dReg, nReg, dJo[tier][0], dJo[tier][1], (unsigned)nJ, mLo, poolG, stG, cnG, B.X, s.dHits, s.dHitCnt, HIT_CAP)
                if (tier == 3) { switch (fusedBlk) { case 64: FUSED_LAUNCH(fusedChainEqKernel, 64); break; case 128: FUSED_LAUNCH(fusedChainEqKernel, 128); break; case 256: FUSED_LAUNCH(fusedChainEqKernel, 256); break; default: FUSED_LAUNCH(fusedChainEqKernel, 512); break; } }
                else { switch (fusedBlk) { case 64: FUSED_LAUNCH(fusedChainKernel, 64); break; case 128: FUSED_LAUNCH(fusedChainKernel, 128); break; case 256: FUSED_LAUNCH(fusedChainKernel, 256); break; default: FUSED_LAUNCH(fusedChainKernel, 512); break; } }
#undef FUSED_LAUNCH
                CUDA_OK(cudaGetLastError());
                CUDA_OK(cudaMemcpyAsync(s.hCnt, s.dHitCnt, 8, cudaMemcpyDeviceToHost, s.st)); CUDA_OK(cudaMemcpyAsync(s.hHits, s.dHits, HCOPY * sizeof(SolveHit), cudaMemcpyDeviceToHost, s.st)); cudaEventRecord(s.e1, s.st);
                s.busy = true; s.bundle = b; totTasks += (unsigned long long)nReg * (unsigned long long)nJ; bst[b].tasks += (unsigned long long)nReg * (unsigned long long)nJ; bst[b].groups++; totGroups++;
                if (!quiet && (totGroups & 15) == 0) fprintf(stderr, "groups %lld..%lld: %llu hits | %.1f s\n", firstIndex, firstIndex + (long long)totGroups, (unsigned long long)totHits, wall());
            }
            gi = batchEnd;
        }
        drainAll();
        { std::unique_lock<std::mutex> lk(qm); qdone.wait(lk, [&] { return inFlight == 0; }); stopW = true; } qcv.notify_all(); for (auto& t : pool) t.join();
        if (dumpHits) fclose(dumpHits); if (dumpTasks) fclose(dumpTasks);
        for (size_t b = 0; b < bundles.size(); b++) if (bst[b].groups) printf("BUNDLE %zu layout %llu regime %d%d tier %d: %lld groups, %llu tasks, %llu hits\n", b, bundles[b].X, bundles[b].rgx, bundles[b].rgz, bundles[b].tier, bst[b].groups, bst[b].tasks, (unsigned long long)bHits[b]);
        printf("solve: %llu tasks in %llu groups, %llu hits (%.4f per task), host-check failures %llu, pool regrows %d, hit-copy overflows %d\n", totTasks, totGroups, (unsigned long long)totHits, totTasks ? (double)totHits / totTasks : 0.0, (unsigned long long)totBad, poolRegrow, hitOverflow);
        printf("solve times: early %.3f s, late %.3f s (%.4f us per task for the solve), host enumeration %.3f s, wait %.3f s; wall %.3f s (%.4f us per task wall)\n", tBuildL, tChain, totTasks ? 1e6 * (tBuildL + tChain) / totTasks : 0.0, tHost, tWait, wall(), totTasks ? 1e6 * wall() / totTasks : 0.0);
        return totBad ? 1 : 0;
    }
    if (solve) {

        const size_t NL = X.size(); const unsigned long long ITEM_CAP = (unsigned long long)(itemCapMi > 1 ? itemCapMi : 1) << 20, HIT_CAP = 1ULL << 16;
        if (taskBatch < 1 || taskBatch > (1 << 19)) { fprintf(stderr, "bad --task-batch (1 .. 524288)\n"); return 2; }
        if ((unsigned long long)taskBatch * 1100ULL > ITEM_CAP) fprintf(stderr, "warning: --task-batch %d needs about %llu Mi items, --item-cap-mi is %d (an overflow stops the run)\n", taskBatch, ((unsigned long long)taskBatch * 1100ULL) >> 20, itemCapMi);
        SolveTask* dTasks; unsigned long long *dItems, *dItemCnt, *dHitCnt; SolveHit* dHits;
        { unsigned h2s[48], h4s[48]; for (int jj = 0; jj < 48; jj++) { const unsigned long long a2 = (jj >= 16) ? ((0x5DEECE66DULL * 0x5DEECE66DULL) & S48_MASK) << (jj - 16) : 0ULL, a4 = (jj >= 16) ? (((((0x5DEECE66DULL * 0x5DEECE66DULL) & S48_MASK) * ((0x5DEECE66DULL * 0x5DEECE66DULL) & S48_MASK)) & S48_MASK)) << (jj - 16) : 0ULL; h2s[jj] = (unsigned)a2; h4s[jj] = (unsigned)a4; } CUDA_OK(cudaMemcpyToSymbol(c_SA2s, h2s, sizeof h2s)); CUDA_OK(cudaMemcpyToSymbol(c_SA4s, h4s, sizeof h4s)); }
        CUDA_OK(cudaMalloc(&dTasks, (size_t)taskBatch * sizeof(SolveTask))); CUDA_OK(cudaMalloc(&dItems, ITEM_CAP * 8)); CUDA_OK(cudaMalloc(&dItemCnt, 16));
        CUDA_OK(cudaMalloc(&dHits, HIT_CAP * sizeof(SolveHit))); CUDA_OK(cudaMalloc(&dHitCnt, 8));
        if (solve4 && !noHut) hutPruneListUpload();
        auto splitmix = [](unsigned long long z) { z += 0x9E3779B97F4A7C15ULL; z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL; z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL; return z ^ (z >> 31); };
        double tEarly = 0, tLate = 0, tTerr = 0; unsigned long long totHits = 0, totBad = 0, totItems = 0, totTasks = 0;
        std::vector<SolveTask> ht;
        std::vector<unsigned long long> tfX; std::vector<int> tfCx, tfCz;
        if (tasksFile) { FILE* tf = fopen(tasksFile, "r"); if (!tf) { fprintf(stderr, "cannot read %s\n", tasksFile); return 1; } unsigned long long x; int a, b; while (fscanf(tf, "%llu %d %d", &x, &a, &b) == 3) { tfX.push_back(x); tfCx.push_back(a); tfCz.push_back(b); } fclose(tf); }
        const long long nTasks = tasksFile ? (long long)tfX.size() : (one ? 1 : count);
        std::vector<SolveTask> htNext; long long nextG0 = -1;
        auto genBatch = [&](long long g0, std::vector<SolveTask>& dst) {
            const int nt = (int)std::min<long long>(taskBatch, nTasks - g0);
            dst.assign(nt, SolveTask());
            for (int i = 0; i < nt; i++) {
                SolveTask& T = dst[i]; long long rx, rz; int ox, oz; size_t li = 0;
                if (tasksFile) { T.X = tfX[g0 + i] & S48_MASK; T.cx = tfCx[g0 + i]; T.cz = tfCz[g0 + i]; rx = T.cx >> 5; rz = T.cz >> 5; T.val = 0; }
                else if (one) { T.X = oneX & S48_MASK; T.cx = (int)oneCx; T.cz = (int)oneCz; rx = oneCx >> 5; rz = oneCz >> 5; T.val = 0; }
                else {
                    const unsigned long long g = (unsigned long long)firstIndex + g0 + i; li = (size_t)(g % NL);
                    const unsigned long long h1 = splitmix(2 * g + 1), h2 = splitmix(2 * g + 2);
                    rx = (long long)(h1 % (2 * radius + 1)) - radius; rz = (long long)((h1 >> 32) % (2 * radius + 1)) - radius; ox = (int)(h2 % 24); oz = (int)((h2 >> 32) % 24);
                    if (regX >= 0) rx = pickRegion(regX, h1, radius);
                    if (regZ >= 0) rz = pickRegion(regZ, h1 >> 32, radius);
                    if (oddTasks) oz = (oz & ~1) | ((ox & 1) ^ 1);
                    T.X = X[li]; T.cx = (int)(rx * 32 + ox); T.cz = (int)(rz * 32 + oz); T.val = V[li];
                }
                T.Creg = (unsigned long long)(14357620LL + rx * 341873128712LL + rz * 132897987541LL) & S48_MASK; T.pad = noHut ? 1u : 0u;
                if (dumpTasks) fprintf(dumpTasks, "%llu %d %d\n", T.X, T.cx, T.cz);
            }
        };
        for (long long g0 = 0; g0 < nTasks; g0 += taskBatch) {
            const int nt = (int)std::min<long long>(taskBatch, nTasks - g0);
            if (nextG0 == g0) ht.swap(htNext); else genBatch(g0, ht);
            nextG0 = -1;
            CUDA_OK(cudaMemcpy(dTasks, ht.data(), nt * sizeof(SolveTask), cudaMemcpyHostToDevice));
            CUDA_OK(cudaMemset(dItemCnt, 0, 16)); CUDA_OK(cudaMemset(dHitCnt, 0, 8));
            const double e0 = lap();
            const unsigned long long nThr = (unsigned long long)nt << 16, HALF = ITEM_CAP / 2;
            unsigned long long ni = 0;
            if (solve3) {
                const unsigned long long capA = oddTasks ? ITEM_CAP : HALF, capB = oddTasks ? 0ULL : HALF;
                if (solve4 && !noHut) solveEarly4Kernel<<<(unsigned)nt, 256>>>(dTasks, dItems, dItemCnt, dItems + capA, dItemCnt + 1, capA, capB);
                else solveEarly3Kernel<<<(unsigned)nt, 256>>>(dTasks, dItems, dItemCnt, dItems + capA, dItemCnt + 1, capA, capB);
                CUDA_OK(cudaGetLastError());
                if (!one && g0 + taskBatch < nTasks) { genBatch(g0 + taskBatch, htNext); nextG0 = g0 + taskBatch; }
                const double e1 = lap(); tEarly += e1 - e0;
                unsigned long long nn[2];CUDA_OK(cudaMemcpy(nn, dItemCnt, 16, cudaMemcpyDeviceToHost));
                if (nn[0] > capA || nn[1] > capB) { fprintf(stderr, "*** solve item buffer overflow (%llu, %llu): lower --task-batch\n", nn[0], nn[1]); return 1; }
                if (nn[0]) { solveLate3AKernel<<<(unsigned)((nn[0] + 127) / 128), 128>>>(dTasks, dItems, nn[0], dHits, dHitCnt, HIT_CAP); CUDA_OK(cudaGetLastError()); }
                if (nn[1]) { solveLate3BKernel<<<(unsigned)((nn[1] + 127) / 128), 128>>>(dTasks, dItems + capA, nn[1], dHits, dHitCnt, HIT_CAP); CUDA_OK(cudaGetLastError()); }
                ni = nn[0] + nn[1];
                const double e2 = lap(); tLate += e2 - e1; totItems += ni;
            } else {
            if (solveOld) solveEarlyKernel<<<(unsigned)((nThr + 255) / 256), 256>>>(dTasks, nThr, dItems, dItemCnt, ITEM_CAP); else solveEarly2Kernel<<<(unsigned)nt, 256>>>(dTasks, dItems, dItemCnt, ITEM_CAP);
            CUDA_OK(cudaGetLastError());
            const double e1 = lap(); tEarly += e1 - e0;
            CUDA_OK(cudaMemcpy(&ni, dItemCnt, 8, cudaMemcpyDeviceToHost));
            if (ni > ITEM_CAP) { fprintf(stderr, "*** solve item buffer overflow (%llu): lower --task-batch\n", ni); return 1; }
            if (ni) { if (solveOld) solveLateKernel<<<(unsigned)((ni + 127) / 128), 128>>>(dTasks, dItems, ni, dHits, dHitCnt, HIT_CAP); else solveLate2Kernel<<<(unsigned)((ni + 127) / 128), 128>>>(dTasks, dItems, ni, dHits, dHitCnt, HIT_CAP); CUDA_OK(cudaGetLastError()); }
            const double e2 = lap(); tLate += e2 - e1; totItems += ni;
            }
            unsigned long long nh = 0; CUDA_OK(cudaMemcpy(&nh, dHitCnt, 8, cudaMemcpyDeviceToHost));
            if (nh > HIT_CAP) { fprintf(stderr, "*** solve hit buffer overflow (%llu)\n", nh); return 1; }
            std::vector<SolveHit> hh(nh); if (nh) CUDA_OK(cudaMemcpy(hh.data(), dHits, nh * sizeof(SolveHit), cudaMemcpyDeviceToHost));
            std::sort(hh.begin(), hh.end(), [](const SolveHit& a, const SolveHit& b) { return a.S != b.S ? a.S < b.S : (a.cx != b.cx ? a.cx < b.cx : a.cz < b.cz); });
            hh.erase(std::unique(hh.begin(), hh.end(), [](const SolveHit& a, const SolveHit& b) { return a.S == b.S && a.cx == b.cx && a.cz == b.cz; }), hh.end());
            totTasks += nt;
            std::vector<long long> pendS; std::vector<Match> pendM;
            for (const SolveHit& h : hh) {

                const RoomCtx hc = makeCtx((int64_t)h.S);
                const unsigned long long Xc = ((unsigned long long)((long long)h.cx * hc.a) ^ (unsigned long long)((long long)h.cz * hc.b) ^ h.S) & S48_MASK;
                int32_t ex, ez; rcHutExact(rcRowBase((int64_t)h.S, h.cx >> 5, h.cz >> 5), h.cx >> 5, h.cz >> 5, &ex, &ez);
                const bool good = Xc == h.X && (noHut || (ex == h.cx && ez == h.cz)) && rcIsStartSeed(hc, (int64_t)h.X);
                if (!good) { totBad++; fprintf(stderr, "*** HOST CHECK FAILED: S=%llu X=%llu chunk (%d,%d)\n", h.S, h.X, h.cx, h.cz); continue; }
                totHits++;
                if (one) { printf("S=%llu  (chunk %d,%d, X=%llu)\n", h.S, h.cx, h.cz, h.X); continue; }
                if (solveOnly) { if (dumpHits) fprintf(dumpHits, "%llu,%d,%d,%llu,%d\n", h.S, h.cx, h.cz, h.X, (int)(ex == h.cx && ez == h.cz)); continue; }
                Match m; m.cx = h.cx; m.cz = h.cz; m.carver = h.X; m.val = h.val;
                if (dumpHits) fprintf(dumpHits, "%llu,%d,%d,%llu,%d\n", h.S, h.cx, h.cz, h.X, (int)(ex == h.cx && ez == h.cz));
                pendS.push_back((long long)h.S); pendM.push_back(m);
            }
            if (!pendS.empty()) { const double q0 = lap(); totSurv += terrainMany(pendS, pendM); tTerr += lap() - q0; pendS.clear(); pendM.clear(); }
            fflush(out);
            if (!quiet) fprintf(stderr, "tasks %lld..%lld: %llu late items, %zu hits (%llu total), %llu terrain survivors | %.1f s\n", firstIndex + g0, firstIndex + g0 + nt, ni, hh.size(), totHits, totSurv, wall());
        }
        fclose(out); if (dumpHits) fclose(dumpHits); if (dumpTasks) fclose(dumpTasks);
        printf("solve: %llu tasks, %llu late items (%.0f per task), %llu hits (%.3f per task), host-check failures %llu, terrain survivors %llu -> %s\n", totTasks, totItems, totTasks ? (double)totItems / totTasks : 0.0, totHits, totTasks ? (double)totHits / totTasks : 0.0, totBad, totSurv, outPath);
        printf("solve times: early %.3f s, late %.3f s (%.1f us per task for the solve), terrain %.3f s over %llu matches (%.1f ms per match); wall %.1f s\n", tEarly, tLate, totTasks ? 1e6 * (tEarly + tLate) / totTasks : 0.0, tTerr, totHits, totHits ? 1e3 * tTerr / totHits : 0.0, wall());
        if (timing) printf("terrain stage inside: build %.3f s (survivors' full caches %.3f s), gate %.3f s\n", tBuild, tAliveB, tGate);
        return totBad ? 1 : 0;
    }
    for (long long si = 0; si < count; si++) {
        const long long s48 = (long long)((((unsigned long long)(firstIndex + si)) * S48_K + S48_SALT) & S48_MASK);
        const RoomCtx ctx = makeCtx(s48);
        CUDA_OK(cudaMemset(dStartCount, 0, 8)); CUDA_OK(cudaMemset(dMatchCnt, 0, 8));
        const int tileRx = 2 * radius + 1;
        const long long threads = (long long)tileRx * nStrips;
        const double l0 = timing ? lap() : 0;

        const unsigned long long b48 = (unsigned long long)ctx.b & S48_MASK;
        int kz = 0; while (kz < 48 && !((b48 >> kz) & 1)) kz++;
        if (invert && kz <= 20) {
            const unsigned long long binv = invOdd(b48 >> kz), mbits = (1ULL << (48 - kz)) - 1;
            const long long nthr = (long long)(2 * radius + 1) * 24;
            invertKernel<<<(unsigned)((nthr + 255) / 256), 256>>>(ctx, dLx, dLv, (int)X.size(), kz, binv, mbits, -radius, 2 * radius + 1, radius, dMatches, dMatchCnt, STARTS_MATCH_CAP);
        } else {
            if (invert && !quiet) fprintf(stderr, "kz=%d > 20 for this structure seed: sweep fallback\n", kz);
            sweepProbeKernel<<<(int)((threads + 255) / 256), 256>>>(ctx, tab, -radius, rzLo, tileRx, nStrips, nRz, dStartCount, dMatches, dMatchCnt, STARTS_MATCH_CAP);
        }
        CUDA_OK(cudaGetLastError());
        if (timing) tSweep += lap() - l0;
        unsigned long long Mn = 0, starts = 0; CUDA_OK(cudaMemcpy(&Mn, dMatchCnt, 8, cudaMemcpyDeviceToHost)); CUDA_OK(cudaMemcpy(&starts, dStartCount, 8, cudaMemcpyDeviceToHost));
        if (Mn > STARTS_MATCH_CAP) { fprintf(stderr, "*** match buffer overflow (%llu)\n", Mn); return 1; }
        std::vector<Match> mm(Mn); if (Mn) CUDA_OK(cudaMemcpy(mm.data(), dMatches, Mn * sizeof(Match), cudaMemcpyDeviceToHost));
        const unsigned long long survS = terrain(s48, Mn, mm, firstIndex + si);
        fflush(out);
        totMatches += Mn; totSurv += survS;
        if (!quiet) fprintf(stderr, "S48 %lld (index %lld): %llu hut starts, %llu catalogue matches, %llu terrain survivors | %.1f s\n", s48, firstIndex + si, starts, Mn, survS, wall());
    }
    fclose(out);
    if (timing) printf("stage times: sweep %.3f s, cache build %.3f s, gate %.3f s (per structure seed: %.3f / %.3f / %.3f)\n", tSweep, tBuild, tGate, tSweep / count, tBuild / count, tGate / count);
    if (profile) {
        unsigned long long h[NPROF]; CUDA_OK(cudaMemcpy(h, dProf, NPROF * 8, cudaMemcpyDeviceToHost));
        const char* nm[] = { "pairs", "erosion>=.55", "temperature", "ridge", "continentalness", "Entrance(y50)<0", "Entrance(y60)<0", "E2&Cheese(maxH)", "ladder y<=0", "ladder y10..40", "aquifer flood", "ALL (marginals AND)", "climate AND", "Entrance50&60", "column gates AND" };
        printf("gate profile over %llu (u,match) pairs, first footprint column, each gate evaluated independently:\n", totPairs);
        for (int i = 0; i < 15; i++) printf("  %-22s pass %12llu  %.3e\n", nm[i], h[i], totPairs ? (double)h[i] / (double)totPairs : 0.0);
    }
    printf("done:%lld structure seeds, %llu matches, %llu survivors -> %s (%.1f s, %.2f s per structure seed)\n", count, totMatches, totSurv, outPath, wall(), wall() / count);
    return 0;
}
