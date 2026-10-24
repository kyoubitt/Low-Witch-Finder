#pragma once
#include <cstdint>

#ifdef __CUDACC__
#define RC_HD __host__ __device__
#else
#define RC_HD
#endif

#define RC_SPACING   32
#define RC_OFFSET    24
#define RC_SALT      14357620LL
#define RC_RX_MULT   341873128712LL
#define RC_RZ_MULT   132897987541LL

#define RC_LCG_MULT  0x5DEECE66DULL
#define RC_LCG_ADD   0xBULL
#define RC_LCG_MASK  ((1ULL << 48) - 1)

#define RC_MINESHAFT_P 0.004

#ifdef __CUDA_ARCH__

__device__ __forceinline__ int32_t rcMod24(int32_t bits) {
    return bits - 24 * (int32_t)(__umulhi((uint32_t)bits, 2863311531U) >> 4);
}
#else
RC_HD inline int32_t rcMod24(int32_t bits) {
    return bits - 24 * (int32_t)(((uint64_t)(uint32_t)bits * 2863311531ULL) >> 36);
}
#endif

struct RoomCtx {
    int64_t seed;
    int64_t a, b;
    int32_t hiReject;
    int32_t hiAcceptBelow;
};

RC_HD inline bool rcIsStartS0(const RoomCtx& c, uint64_t s) {
    s = (s * RC_LCG_MULT + RC_LCG_ADD) & RC_LCG_MASK;
    int32_t hi = (int32_t)(s >> 22);
    if (hi >= c.hiReject) return false;
    if (hi < c.hiAcceptBelow) return true;
    s = (s * RC_LCG_MULT + RC_LCG_ADD) & RC_LCG_MASK;
    int32_t lo = (int32_t)(s >> 21);
    return (double)(((int64_t)hi << 27) + (int64_t)lo) * 1.1102230246251565404e-16
            < RC_MINESHAFT_P;
}

RC_HD inline bool rcIsStartSeed(const RoomCtx& c, int64_t carver) {
    return rcIsStartS0(c, ((uint64_t)carver ^ RC_LCG_MULT) & RC_LCG_MASK);
}

RC_HD inline int64_t rcCarverSeed(const RoomCtx& c, int32_t cx, int32_t cz) {
    return (int64_t)((uint64_t)((int64_t)cx * c.a) ^ (uint64_t)((int64_t)cz * c.b)
                     ^ (uint64_t)c.seed);
}

RC_HD inline bool rcIsStart(const RoomCtx& c, int32_t cx, int32_t cz) {
    return rcIsStartSeed(c, rcCarverSeed(c, cx, cz));
}

RC_HD inline bool rcIsStartSeedReference(int64_t carver) {
    uint64_t s = ((uint64_t)carver ^ RC_LCG_MULT) & RC_LCG_MASK;
    s = (s * RC_LCG_MULT + RC_LCG_ADD) & RC_LCG_MASK;
    int32_t hi = (int32_t)(s >> 22);
    s = (s * RC_LCG_MULT + RC_LCG_ADD) & RC_LCG_MASK;
    int32_t lo = (int32_t)(s >> 21);
    return (double)(((int64_t)hi << 27) + (int64_t)lo) * 1.1102230246251565404e-16
            < RC_MINESHAFT_P;
}

RC_HD inline int32_t rcFirstDraw(int64_t carver) {
    uint64_t s = ((uint64_t)carver ^ RC_LCG_MULT) & RC_LCG_MASK;
    s = (s * RC_LCG_MULT + RC_LCG_ADD) & RC_LCG_MASK;
    return (int32_t)(s >> 22);
}

RC_HD inline int64_t rcRowBase(int64_t worldSeed, int32_t rx, int32_t rz) {
    return (int64_t)((uint64_t)worldSeed + (uint64_t)RC_SALT
                     + (uint64_t)((int64_t)rx * RC_RX_MULT)
                     + (uint64_t)((int64_t)rz * RC_RZ_MULT));
}

RC_HD inline void rcHutExact(int64_t regionSeed, int32_t rx, int32_t rz,
                             int32_t* cx, int32_t* cz) {
    uint64_t s = ((uint64_t)regionSeed ^ RC_LCG_MULT) & RC_LCG_MASK;
    int32_t bits, val;
    do {
        s = (s * RC_LCG_MULT + RC_LCG_ADD) & RC_LCG_MASK;
        bits = (int32_t)(s >> 17);
        val = bits % RC_OFFSET;
    } while (bits - val + (RC_OFFSET - 1) < 0);
    int32_t ox = val;
    do {
        s = (s * RC_LCG_MULT + RC_LCG_ADD) & RC_LCG_MASK;
        bits = (int32_t)(s >> 17);
        val = bits % RC_OFFSET;
    } while (bits - val + (RC_OFFSET - 1) < 0);
    *cx = (int32_t)((uint32_t)rx * (uint32_t)RC_SPACING + (uint32_t)ox);
    *cz = (int32_t)((uint32_t)rz * (uint32_t)RC_SPACING + (uint32_t)val);
}

RC_HD inline void rcHut(int64_t regionSeed, int32_t rx, int32_t rz,
                        int32_t* cx, int32_t* cz) {
    uint64_t s = ((uint64_t)regionSeed ^ RC_LCG_MULT) & RC_LCG_MASK;
    s = (s * RC_LCG_MULT + RC_LCG_ADD) & RC_LCG_MASK;
    int32_t b1 = (int32_t)(s >> 17);
    int32_t v1 = rcMod24(b1);
    s = (s * RC_LCG_MULT + RC_LCG_ADD) & RC_LCG_MASK;
    int32_t b2 = (int32_t)(s >> 17);
    int32_t v2 = rcMod24(b2);
    if ((b1 - v1 + (RC_OFFSET - 1)) < 0 || (b2 - v2 + (RC_OFFSET - 1)) < 0) {
        rcHutExact(regionSeed, rx, rz, cx, cz);
        return;
    }
    *cx = (int32_t)((uint32_t)rx * (uint32_t)RC_SPACING + (uint32_t)v1);
    *cz = (int32_t)((uint32_t)rz * (uint32_t)RC_SPACING + (uint32_t)v2);
}

RC_HD inline uint64_t rcPack(int32_t cx, int32_t cz) {
    return ((uint64_t)(uint32_t)cx << 32) | (uint64_t)(uint32_t)cz;
}

RC_HD inline int32_t rcUnpackX(uint64_t p) { return (int32_t)(uint32_t)(p >> 32); }
RC_HD inline int32_t rcUnpackZ(uint64_t p) { return (int32_t)(uint32_t)p; }

struct RoomWalker {
    uint64_t rs;
    uint64_t pz;
    uint64_t px;
    int32_t  rx, rz;
};

RC_HD inline void rcWalkInit(RoomWalker& w, const RoomCtx& c, int32_t rx, int32_t rz) {
    w.rs = (uint64_t)rcRowBase(c.seed, rx, rz);
    w.px = (uint64_t)((int64_t)((int32_t)((uint32_t)rx * 32U))) * (uint64_t)c.a;
    w.pz = (uint64_t)((int64_t)((int32_t)((uint32_t)rz * 32U))) * (uint64_t)c.b;
    w.rx = rx;
    w.rz = rz;
#ifdef __CUDA_ARCH__

    asm volatile("" : "+l"(w.px), "+l"(w.pz));
#endif
}

template <uint32_t RETRY_BOUND = 0x7FFFFFE9U>
RC_HD inline bool rcWalkStep(RoomWalker& w, const RoomCtx& c, uint64_t K,
                             int32_t* cx, int32_t* cz) {
    uint64_t s = (w.rs ^ RC_LCG_MULT) & RC_LCG_MASK;
    s = (s * RC_LCG_MULT + RC_LCG_ADD) & RC_LCG_MASK;
    const uint32_t b1 = (uint32_t)(s >> 17);
    s = (s * RC_LCG_MULT + RC_LCG_ADD) & RC_LCG_MASK;
    const uint32_t b2 = (uint32_t)(s >> 17);
    uint32_t v1, v2;
    const uint32_t bmax = b1 > b2 ? b1 : b2;
    if (bmax >= RETRY_BOUND) {

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
    const bool start = rcIsStartS0(c, ((X ^ Z) & RC_LCG_MASK) ^ K);

    *cx = (int32_t)((uint32_t)w.rx * 32U + v1);
    *cz = (int32_t)((uint32_t)w.rz * 32U + v2);
    w.rs += (uint64_t)RC_RZ_MULT;
    w.pz += 32ULL * (uint64_t)c.b;
    w.rz += 1;
    return start;
}

RC_HD inline uint64_t rcWalkK(const RoomCtx& c) {
    return ((uint64_t)c.seed ^ RC_LCG_MULT) & RC_LCG_MASK;
}
