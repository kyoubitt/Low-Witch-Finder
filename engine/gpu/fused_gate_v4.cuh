#pragma once
#include <cstddef>
#include "device_noise.h"
#include "device_construct.h"

#define SH_BLK2 128
#define B1_SLACK 1e-5

__device__ __forceinline__ uint32_t xor3(uint32_t a, uint32_t b, uint32_t c) { uint32_t d; asm("lop3.b32 %0, %1, %2, %3, 0x96;" : "=r"(d) : "r"(a), "r"(b), "r"(c)); return d; }
struct Rng32 { uint32_t s0l, s0h, s1l, s1h; };
__device__ __forceinline__ uint32_t xrNextLow32(Rng32& g) {
    const uint32_t tl = g.s0l + g.s1l, th = g.s0h + g.s1h + (tl < g.s0l ? 1u : 0u);
    const uint32_t res = __funnelshift_l(th, tl, 17) + g.s0l;
    const uint32_t yl = g.s0l ^ g.s1l, yh = g.s0h ^ g.s1h;
    const uint32_t rl = __funnelshift_l(g.s0l, g.s0h, 17), rh = __funnelshift_l(g.s0h, g.s0l, 17);
    const uint32_t sl = yl << 21, shh = __funnelshift_l(yl, yh, 21);
    g.s0l = xor3(rl, yl, sl); g.s0h = xor3(rh, yh, shh);
    g.s1l = __funnelshift_l(yh, yl, 28); g.s1h = __funnelshift_l(yl, yh, 28);
    return res;
}
__device__ __forceinline__ int xrNextInt(Rng32& g, uint32_t bound, uint32_t i) {
    uint64_t r = (uint64_t)xrNextLow32(g) * bound;
    uint32_t low = (uint32_t)r;
    if (low < bound) {
        const uint32_t threshold = (0u - bound) % bound;
        while (low < threshold) { r = (uint64_t)xrNextLow32(g) * bound; low = (uint32_t)r; }
    }
    return (int)(i + (uint32_t)(r >> 32));
}
template <int J>
__device__ __forceinline__ void shStep(Rng32& rng, unsigned* __restrict__ sh, int lane, int g, unsigned& W) {
    const uint32_t i = 4 * g + J;
    const int k = xrNextInt(rng, 256u - i, i);
    const unsigned cur = __byte_perm(W, 0u, 0x4440u | J);
    const int kw = k >> 2;
    const bool same = (kw == g);
    const unsigned wk = sh[kw * 32 + lane];
    const unsigned T = same ? W : wk;
    const unsigned R = __funnelshift_r(T, T, (unsigned)k << 3);
    const unsigned bk = R & 255u;
    const unsigned R2 = __byte_perm(R, cur, 0x3214u);
    const unsigned Tn = __funnelshift_l(R2, R2, (unsigned)k << 3);
    sh[kw * 32 + lane] = Tn;
    const unsigned Wb = same ? Tn : W;
    const unsigned selW = (J == 0) ? 0x3214u : (J == 1) ? 0x3240u : (J == 2) ? 0x3410u : 0x4210u;
    W = __byte_perm(Wb, bk, selW);
}
__device__ __forceinline__ uint32_t xrNextLow32c(Rng32& g) {
    uint32_t tl, th;
    asm("add.cc.u32 %0, %2, %3; addc.u32 %1, %4, %5;" : "=r"(tl), "=r"(th) : "r"(g.s0l), "r"(g.s1l), "r"(g.s0h), "r"(g.s1h));
    const uint32_t res = __funnelshift_l(th, tl, 17) + g.s0l;
    const uint32_t yl = g.s0l ^ g.s1l, yh = g.s0h ^ g.s1h;
    const uint32_t rl = __funnelshift_l(g.s0l, g.s0h, 17), rh = __funnelshift_l(g.s0h, g.s0l, 17);
    const uint32_t sl = yl << 21, shh = __funnelshift_l(yl, yh, 21);
    g.s0l = xor3(rl, yl, sl); g.s0h = xor3(rh, yh, shh);
    g.s1l = __funnelshift_l(yh, yl, 28); g.s1h = __funnelshift_l(yl, yh, 28);
    return res;
}
template <int J>
__device__ __forceinline__ void shSwap(int k, unsigned* __restrict__ sh, int lane, int g, unsigned& W) {
    const int kw = k >> 2; const bool same = (kw == g);
    const unsigned cur = __byte_perm(W, 0u, 0x4440u | J);
    const unsigned wk = sh[kw * 32 + lane]; const unsigned T = same ? W : wk;
    const unsigned R = __funnelshift_r(T, T, (unsigned)k << 3);
    const unsigned bk = R & 255u, R2 = __byte_perm(R, cur, 0x3214u);
    const unsigned Tn = __funnelshift_l(R2, R2, (unsigned)k << 3);
    sh[kw * 32 + lane] = Tn;
    const unsigned Wb = same ? Tn : W;
    const unsigned selW = (J == 0) ? 0x3214u : (J == 1) ? 0x3240u : (J == 2) ? 0x3410u : 0x4210u;
    W = __byte_perm(Wb, bk, selW);
}
__device__ __forceinline__ void shShuffle256(Xoroshiro128PP& src, unsigned* __restrict__ sh, int lane) {
    Rng32 rng; rng.s0l = (uint32_t)src.loSeed; rng.s0h = (uint32_t)(src.loSeed >> 32); rng.s1l = (uint32_t)src.hiSeed; rng.s1h = (uint32_t)(src.hiSeed >> 32);
#pragma unroll
    for (int w = 0; w < 64; w++) sh[w * 32 + lane] = 0x03020100u + 0x04040404u * (unsigned)w;

    for (int g2 = 0; g2 < 32; g2++) {
        const int ga = 2 * g2, gb = ga + 1; const uint32_t b0 = 256u - 8 * (uint32_t)g2;
        const Rng32 save = rng;
        const uint64_t r0 = (uint64_t)xrNextLow32c(rng) * b0, r1 = (uint64_t)xrNextLow32c(rng) * (b0 - 1), r2 = (uint64_t)xrNextLow32c(rng) * (b0 - 2), r3 = (uint64_t)xrNextLow32c(rng) * (b0 - 3);
        const uint64_t r4 = (uint64_t)xrNextLow32c(rng) * (b0 - 4), r5 = (uint64_t)xrNextLow32c(rng) * (b0 - 5), r6 = (uint64_t)xrNextLow32c(rng) * (b0 - 6), r7 = (uint64_t)xrNextLow32c(rng) * (b0 - 7);
        const bool rej = ((uint32_t)r0 < b0) | ((uint32_t)r1 < b0 - 1) | ((uint32_t)r2 < b0 - 2) | ((uint32_t)r3 < b0 - 3) | ((uint32_t)r4 < b0 - 4) | ((uint32_t)r5 < b0 - 5) | ((uint32_t)r6 < b0 - 6) | ((uint32_t)r7 < b0 - 7);
        unsigned Wa = sh[ga * 32 + lane];
        if (__builtin_expect(rej, 0)) {
            rng = save;
            shStep<0>(rng, sh, lane, ga, Wa); shStep<1>(rng, sh, lane, ga, Wa); shStep<2>(rng, sh, lane, ga, Wa); shStep<3>(rng, sh, lane, ga, Wa); sh[ga * 32 + lane] = Wa;
            unsigned Wb = sh[gb * 32 + lane];
            shStep<0>(rng, sh, lane, gb, Wb); shStep<1>(rng, sh, lane, gb, Wb); shStep<2>(rng, sh, lane, gb, Wb); shStep<3>(rng, sh, lane, gb, Wb); sh[gb * 32 + lane] = Wb;
        } else {
            const int base = 8 * g2;
            shSwap<0>(base + 0 + (int)(uint32_t)(r0 >> 32), sh, lane, ga, Wa); shSwap<1>(base + 1 + (int)(uint32_t)(r1 >> 32), sh, lane, ga, Wa);
            shSwap<2>(base + 2 + (int)(uint32_t)(r2 >> 32), sh, lane, ga, Wa); shSwap<3>(base + 3 + (int)(uint32_t)(r3 >> 32), sh, lane, ga, Wa);
            sh[ga * 32 + lane] = Wa;
            unsigned Wb = sh[gb * 32 + lane];
            shSwap<0>(base + 4 + (int)(uint32_t)(r4 >> 32), sh, lane, gb, Wb); shSwap<1>(base + 5 + (int)(uint32_t)(r5 >> 32), sh, lane, gb, Wb);
            shSwap<2>(base + 6 + (int)(uint32_t)(r6 >> 32), sh, lane, gb, Wb); shSwap<3>(base + 7 + (int)(uint32_t)(r7 >> 32), sh, lane, gb, Wb);
            sh[gb * 32 + lane] = Wb;
        }
    }
}

struct ShPerm {
    const unsigned* sh; int lane; double ox, oy, oz; int iy0; double fy0;
    __device__ __forceinline__ int g(int x) const { const int k = x & 255; return (int)((sh[((k >> 2) << 5) + lane] >> ((k & 3) << 3)) & 255u); }
};
__device__ __forceinline__ void shTableBuild(Xoroshiro128PP& source, unsigned* __restrict__ sh, int lane, ShPerm& p) {
    p.sh = sh; p.lane = lane;
    p.ox = source.nextDouble() * 256.0; p.oy = source.nextDouble() * 256.0; p.oz = source.nextDouble() * 256.0;
    p.iy0 = (int)floor(p.oy); p.fy0 = p.oy - p.iy0;
    shShuffle256(source, sh, lane);
}
__device__ __forceinline__ double perlinSh(const ShPerm& p, double x, double y, double z) {
    double x2 = x + p.ox, y2 = y + p.oy, z2 = z + p.oz;
    int ix = (int)floor(x2), iy = (int)floor(y2), iz = (int)floor(z2);
    dn_core_t fx = (dn_core_t)(x2 - ix), fy = (dn_core_t)(y2 - iy), fz = (dn_core_t)(z2 - iz);
    const dn_core_t ONE = DN_C(1.0);
    int a  = p.g(ix);
    int a1 = p.g(ix + 1);
    int b  = p.g(a + iy);
    int b1 = p.g(a + iy + 1);
    int c  = p.g(a1 + iy);
    int c1 = p.g(a1 + iy + 1);
    dn_core_t v000 = dnc_grad(p.g(b + iz), fx, fy, fz);
    dn_core_t v100 = dnc_grad(p.g(c + iz), fx - ONE, fy, fz);
    dn_core_t v010 = dnc_grad(p.g(b1 + iz), fx, fy - ONE, fz);
    dn_core_t v110 = dnc_grad(p.g(c1 + iz), fx - ONE, fy - ONE, fz);
    dn_core_t v001 = dnc_grad(p.g(b + iz + 1), fx, fy, fz - ONE);
    dn_core_t v101 = dnc_grad(p.g(c + iz + 1), fx - ONE, fy, fz - ONE);
    dn_core_t v011 = dnc_grad(p.g(b1 + iz + 1), fx, fy - ONE, fz - ONE);
    dn_core_t v111 = dnc_grad(p.g(c1 + iz + 1), fx - ONE, fy - ONE, fz - ONE);
    dn_core_t tx = dnc_fade(fx), ty = dnc_fade(fy), tz = dnc_fade(fz);
    return (double)dnc_lerp(tz, dnc_lerp(ty, dnc_lerp(tx, v000, v100), dnc_lerp(tx, v010, v110)),
                                dnc_lerp(ty, dnc_lerp(tx, v001, v101), dnc_lerp(tx, v011, v111)));
}
#ifdef __CUDA_ARCH__
__device__ __forceinline__ double perlinShY0(const ShPerm& p, double x, double z) {
    double x2 = x + p.ox, z2 = z + p.oz;
    int ix, iz; dn_core_t fx, fz;
    dn_split(x2, ix, fx);
    dn_split(z2, iz, fz);
    const int iy = p.iy0;
    dn_core_t fy = (dn_core_t)p.fy0;
    const dn_core_t ONE = DN_C(1.0);
    int a  = p.g(ix);
    int a1 = p.g(ix + 1);
    int b  = p.g(a + iy);
    int b1 = p.g(a + iy + 1);
    int c  = p.g(a1 + iy);
    int c1 = p.g(a1 + iy + 1);
    dn_core_t v000 = dnc_grad(p.g(b + iz), fx, fy, fz);
    dn_core_t v100 = dnc_grad(p.g(c + iz), fx - ONE, fy, fz);
    dn_core_t v010 = dnc_grad(p.g(b1 + iz), fx, fy - ONE, fz);
    dn_core_t v110 = dnc_grad(p.g(c1 + iz), fx - ONE, fy - ONE, fz);
    dn_core_t v001 = dnc_grad(p.g(b + iz + 1), fx, fy, fz - ONE);
    dn_core_t v101 = dnc_grad(p.g(c + iz + 1), fx - ONE, fy, fz - ONE);
    dn_core_t v011 = dnc_grad(p.g(b1 + iz + 1), fx, fy - ONE, fz - ONE);
    dn_core_t v111 = dnc_grad(p.g(c1 + iz + 1), fx - ONE, fy - ONE, fz - ONE);
    dn_core_t tx = dnc_fade(fx), ty = dnc_fade(fy), tz = dnc_fade(fz);
    return (double)dnc_lerp(tz, dnc_lerp(ty, dnc_lerp(tx, v000, v100), dnc_lerp(tx, v010, v110)),
                                dnc_lerp(ty, dnc_lerp(tx, v001, v101), dnc_lerp(tx, v011, v111)));
}
#else
__device__ __forceinline__ double perlinShY0(const ShPerm&, double, double) { return 0.0; }
#endif

template <int N>
__device__ __forceinline__ void fusedSample(const Xoroshiro128PP& deriver, const uint8_t* digest, int firstOctave, const double* amplitudes, int count, unsigned* sh, int lane,
                                            const double* X, const double* Y, const double* Z, double* out) {
    Xoroshiro128PP r = Xoroshiro128PP::createRandomFromDigest(deriver, digest);
    const double f = 1.0181268882175227;
    double acc[2][N];
#pragma unroll
    for (int s = 0; s < 2; s++) for (int n = 0; n < N; n++) acc[s][n] = 0.0;
    for (int s = 0; s < 2; s++) {
        Xoroshiro128PP octaveDeriver = r.createRandomDeriver();
        double freq = pow(2.0, firstOctave), amp = pow(2.0, count - 1) / (pow(2.0, count) - 1.0);
        for (int i = 0; i < count; i++) {
            if (amplitudes[i] != 0.0) {
                Xoroshiro128PP per = Xoroshiro128PP::createRandomFromDigest(octaveDeriver, dev_octaveDigest(firstOctave + i));
                ShPerm p; shTableBuild(per, sh, lane, p);
#pragma unroll
                for (int n = 0; n < N; n++) {
                    const double xs = s ? X[n] * f : X[n], ys = s ? Y[n] * f : Y[n], zs = s ? Z[n] * f : Z[n];
                    double sx = pn_maintainPrecision(xs * freq);
                    double sy = pn_maintainPrecision(ys * freq);
                    double sz = pn_maintainPrecision(zs * freq);
                    acc[s][n] += amplitudes[i] * perlinSh(p, sx, sy, sz) * amp;
                }
            }
            freq *= 2.0; amp /= 2.0;
        }
    }
    int minOctave = 2147483647, maxOctave = -2147483648;
    for (int q = 0; q < count; q++) { if (amplitudes[q] == 0.0) continue; if (q < minOctave) minOctave = q; if (q > maxOctave) maxOctave = q; }
    const double createAmp_n = maxOctave - minOctave;
    const double createAmplitude = 0.1 * (1.0 + 1.0 / (createAmp_n + 1.0));
    const double amplitude = 0.16666666666666666 / createAmplitude;
#pragma unroll
    for (int n = 0; n < N; n++) out[n] = (acc[0][n] + acc[1][n]) * amplitude;
}

enum { S_CE = 0, S_RAR, S_THK, S_L1, S_L2, S_MOD, S_ROU, S_ERO, S_TEM, S_CON, S_RID, S_LAY, S_CHE, S_AQ, S_COUNT };
struct TabItem { int dstOff; int firstOctave; unsigned char samp, set, oct, meta; };

__device__ __forceinline__ const uint8_t* tabDigest(int s) {
    switch (s) {
        case S_CE: return DIGEST_cave_entrance;       case S_RAR: return DIGEST_spaghetti_3d_rarity;     case S_THK: return DIGEST_spaghetti_3d_thickness;
        case S_L1: return DIGEST_spaghetti_3d_1;      case S_L2: return DIGEST_spaghetti_3d_2;           case S_MOD: return DIGEST_spaghetti_roughness_modulator;
        case S_ROU: return DIGEST_spaghetti_roughness; case S_ERO: return DIGEST_erosion;                case S_TEM: return DIGEST_temperature;
        case S_CON: return DIGEST_continentalness;    case S_RID: return DIGEST_ridge;                   case S_LAY: return DIGEST_cave_layer;
        case S_CHE: return DIGEST_cave_cheese;        default: return DIGEST_aquifer_fluid_level_floodedness;
    }
}

__device__ __forceinline__ void fillMetaOctave(DevOctave* o, int firstOctave, const double* amplitudes, int count) {
    o->octaveCount = count;
    o->lacunarity = pow(2.0, firstOctave);
    o->persistence = pow(2.0, count - 1) / (pow(2.0, count) - 1.0);
    double lf = o->lacunarity, la = o->persistence;
    for (int i = 0; i < count; i++) {
        o->amplitudes[i] = amplitudes[i];
        o->hasOctave[i] = (amplitudes[i] != 0.0);
        o->freqAt[i] = lf; o->ampAt[i] = la;
        lf *= 2.0; la /= 2.0;
    }
}
__device__ __forceinline__ void fillMeta(DevDoublePerlin* d, int firstOctave, const double* amplitudes, int count) {
    fillMetaOctave(&d->first, firstOctave, amplitudes, count);
    fillMetaOctave(&d->second, firstOctave, amplitudes, count);
    int minOctave = 2147483647, maxOctave = -2147483648;
    for (int i = 0; i < count; i++) { if (amplitudes[i] == 0.0) continue; if (i < minOctave) minOctave = i; if (i > maxOctave) maxOctave = i; }
    const double createAmp_n = maxOctave - minOctave;
    const double createAmplitude = 0.1 * (1.0 + 1.0 / (createAmp_n + 1.0));
    d->amplitude = 0.16666666666666666 / createAmplitude;
}
__device__ __forceinline__ void fillMetaFor(int s, DevNoiseCache* c) {
    switch (s) {
        case S_CE:  { const double amp[] = {0.4, 0.5, 1.0}; fillMeta(&c->caveEntrance, -7, amp, 3); } break;
        case S_RAR: { const double amp[] = {1.0}; fillMeta(&c->spaghettiRarity, -11, amp, 1); } break;
        case S_THK: { const double amp[] = {1.0}; fillMeta(&c->spaghettiThickness, -8, amp, 1); } break;
        case S_L1:  { const double amp[] = {1.0}; fillMeta(&c->spaghetti3D1, -7, amp, 1); } break;
        case S_L2:  { const double amp[] = {1.0}; fillMeta(&c->spaghetti3D2, -7, amp, 1); } break;
        case S_MOD: { const double amp[] = {1.0}; fillMeta(&c->spaghettiRoughnessModulator, -8, amp, 1); } break;
        case S_ROU: { const double amp[] = {1.0}; fillMeta(&c->spaghettiRoughness, -5, amp, 1); } break;
        case S_ERO: { const double amp[] = {1.0, 1.0, 0.0, 1.0, 1.0}; fillMeta(&c->erosion, -9, amp, 5); } break;
        case S_TEM: { const double amp[] = {1.5, 0.0, 1.0, 0.0, 0.0, 0.0}; fillMeta(&c->temperature, -10, amp, 6); } break;
        case S_CON: { const double amp[] = {1.0, 1.0, 2.0, 2.0, 2.0, 1.0, 1.0, 1.0, 1.0}; fillMeta(&c->continentalness, -9, amp, 9); } break;
        case S_RID: { const double amp[] = {1.0, 2.0, 1.0, 0.0, 0.0, 0.0}; fillMeta(&c->ridge, -7, amp, 6); } break;
        case S_LAY: { const double amp[] = {1.0}; fillMeta(&c->caveLayer, -8, amp, 1); } break;
        case S_CHE: { const double amp[] = {0.5, 1.0, 2.0, 1.0, 2.0, 1.0, 0.0, 2.0, 0.0}; fillMeta(&c->caveCheese, -8, amp, 9); } break;
        default:    { const double amp[] = {1.0}; fillMeta(&c->aquiferFloodedness, -7, amp, 1); } break;
    }
}
__device__ __forceinline__ void storePerlin(DevPerlin* d, const ShPerm& p, const unsigned* sh, int lane) {
    d->originX = p.ox; d->originY = p.oy; d->originZ = p.oz;
    d->iy0 = p.iy0; d->fy0 = p.fy0;
    unsigned long long* t = reinterpret_cast<unsigned long long*>(d->permutations);
#pragma unroll
    for (int w = 0; w < 32; w++) t[w] = (unsigned long long)sh[(2 * w) * 32 + lane] | ((unsigned long long)sh[(2 * w + 1) * 32 + lane] << 32);
}

__global__ void __launch_bounds__(SH_BLK2) parBuildKernel(long long s48, const unsigned* __restrict__ ulist, const int* __restrict__ slotList, const unsigned long long* __restrict__ nPtr, unsigned long long nCap,
                                                          const TabItem* __restrict__ items, int nItems, DevNoiseCache* __restrict__ caches, const long long* __restrict__ s48s = nullptr) {
    __shared__ unsigned shAll[(SH_BLK2 / 32) * 64 * 32];
    const unsigned long long t = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
    const unsigned long long v = t / (unsigned)nItems; const int it = (int)(t % (unsigned)nItems);
    const unsigned long long n = nPtr ? (*nPtr < nCap ? *nPtr : nCap) : nCap;
    if (v >= n) return;
    unsigned* sh = shAll + (threadIdx.x >> 5) * (64 * 32); const int lane = threadIdx.x & 31;
    const TabItem I = items[it];
    const unsigned entry = ulist[v]; const unsigned u = entry & 0xFFFFu;
    const unsigned long long slot = slotList ? (unsigned long long)slotList[v] : v;
    const long long ws = (long long)(((unsigned long long)u << 48) | (unsigned long long)(s48s ? s48s[entry >> 16] : s48));
    Xoroshiro128PP base = Xoroshiro128PP::fromSingleSeed(ws);
    Xoroshiro128PP deriver = base.createRandomDeriver();
    Xoroshiro128PP r = Xoroshiro128PP::createRandomFromDigest(deriver, tabDigest(I.samp));
    Xoroshiro128PP od0 = r.createRandomDeriver();
    Xoroshiro128PP od1 = r.createRandomDeriver();
    Xoroshiro128PP per = Xoroshiro128PP::createRandomFromDigest(I.set ? od1 : od0, dev_octaveDigest(I.firstOctave + I.oct));
    ShPerm p; shTableBuild(per, sh, lane, p);
    char* cbase = reinterpret_cast<char*>(&caches[slot]);
    storePerlin(reinterpret_cast<DevPerlin*>(cbase + I.dstOff), p, sh, lane);
    if (I.meta) fillMetaFor(I.samp, reinterpret_cast<DevNoiseCache*>(cbase));
}

#include <vector>
static size_t samplerOffset(int s) {
    switch (s) {
        case S_CE: return offsetof(DevNoiseCache, caveEntrance);   case S_RAR: return offsetof(DevNoiseCache, spaghettiRarity);   case S_THK: return offsetof(DevNoiseCache, spaghettiThickness);
        case S_L1: return offsetof(DevNoiseCache, spaghetti3D1);   case S_L2: return offsetof(DevNoiseCache, spaghetti3D2);       case S_MOD: return offsetof(DevNoiseCache, spaghettiRoughnessModulator);
        case S_ROU: return offsetof(DevNoiseCache, spaghettiRoughness); case S_ERO: return offsetof(DevNoiseCache, erosion);      case S_TEM: return offsetof(DevNoiseCache, temperature);
        case S_CON: return offsetof(DevNoiseCache, continentalness); case S_RID: return offsetof(DevNoiseCache, ridge);          case S_LAY: return offsetof(DevNoiseCache, caveLayer);
        case S_CHE: return offsetof(DevNoiseCache, caveCheese);    default: return offsetof(DevNoiseCache, aquiferFloodedness);
    }
}

static std::vector<TabItem> makeTabItems(const std::vector<int>& samplers) {
    struct D { int first; std::vector<double> amp; };
    static const D desc[S_COUNT] = {
        { -7,  {0.4, 0.5, 1.0} }, { -11, {1.0} }, { -8, {1.0} }, { -7, {1.0} }, { -7, {1.0} }, { -8, {1.0} }, { -5, {1.0} },
        { -9,  {1.0, 1.0, 0.0, 1.0, 1.0} }, { -10, {1.5, 0.0, 1.0, 0.0, 0.0, 0.0} }, { -9, {1.0, 1.0, 2.0, 2.0, 2.0, 1.0, 1.0, 1.0, 1.0} }, { -7, {1.0, 2.0, 1.0, 0.0, 0.0, 0.0} },
        { -8,  {1.0} }, { -8, {0.5, 1.0, 2.0, 1.0, 2.0, 1.0, 0.0, 2.0, 0.0} }, { -7, {1.0} } };
    std::vector<TabItem> v;
    for (int s : samplers) {
        bool firstOfSampler = true;
        for (int set = 0; set < 2; set++)
            for (int j = 0; j < (int)desc[s].amp.size(); j++) {
                if (desc[s].amp[j] == 0.0) continue;
                TabItem t; t.samp = (unsigned char)s; t.set = (unsigned char)set; t.oct = (unsigned char)j; t.firstOctave = desc[s].first;
                t.dstOff = (int)(samplerOffset(s) + (set ? offsetof(DevDoublePerlin, second) : offsetof(DevDoublePerlin, first)) + offsetof(DevOctave, samplers) + (size_t)j * sizeof(DevPerlin));
                t.meta = firstOfSampler ? 1 : 0; firstOfSampler = false;
                v.push_back(t);
            }
    }
    return v;
}
