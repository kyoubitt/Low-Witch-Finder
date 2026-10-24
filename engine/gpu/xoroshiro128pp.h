#pragma once
#include <cstdint>
#include <cstring>
#include <string>
#include "md5.h"

#ifdef __CUDACC__
#define XRPP_HD __host__ __device__
#else
#define XRPP_HD
#endif

struct Xoroshiro128PP {
    uint64_t loSeed;
    uint64_t hiSeed;

    XRPP_HD static uint64_t rotl(uint64_t x, int bits) {
        return (x << bits) | (x >> (64 - bits));
    }

    XRPP_HD static uint64_t mixStafford13(uint64_t z) {
        z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
        z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
        return z ^ (z >> 31);
    }

    XRPP_HD static Xoroshiro128PP fromSingleSeed(int64_t seed) {
        uint64_t s = (uint64_t)seed;
        uint64_t lo = s ^ 0x6a09e667f3bcc909ULL;
        uint64_t hi = lo - 7046029254386353131ULL;
        Xoroshiro128PP r;
        r.loSeed = mixStafford13(lo);
        r.hiSeed = mixStafford13(hi);
        return r;
    }

    XRPP_HD static Xoroshiro128PP fromRaw(uint64_t lo, uint64_t hi) {
        Xoroshiro128PP r;
        if ((lo | hi) == 0) {
            r.loSeed = (uint64_t)(-7046029254386353131LL);
            r.hiSeed = 7640891576956012809ULL;
        } else {
            r.loSeed = lo;
            r.hiSeed = hi;
        }
        return r;
    }

    XRPP_HD uint64_t nextLong() {
        uint64_t l0 = loSeed;
        uint64_t l1 = hiSeed;
        uint64_t result = rotl(l0 + l1, 17) + l0;
        l1 ^= l0;
        loSeed = rotl(l0, 49) ^ l1 ^ (l1 << 21);
        hiSeed = rotl(l1, 28);
        return result;
    }

    XRPP_HD uint64_t nextBits(int bits) {
        return nextLong() >> (64 - bits);
    }

    XRPP_HD int32_t nextInt() {
        return (int32_t)(int64_t)nextBits(32);
    }

    XRPP_HD int32_t nextInt(int32_t bound) {
        uint64_t r = (nextLong() & 0xFFFFFFFFULL) * (uint64_t)(uint32_t)bound;
        uint64_t low = r & 0xFFFFFFFFULL;
        if (low < (uint64_t)(uint32_t)bound) {
            uint32_t negBound = (uint32_t)(~bound + 1);
            uint32_t threshold = negBound % (uint32_t)bound;
            while (low < (uint64_t)threshold) {
                r = (nextLong() & 0xFFFFFFFFULL) * (uint64_t)(uint32_t)bound;
                low = r & 0xFFFFFFFFULL;
            }
        }
        return (int32_t)(r >> 32);
    }

    XRPP_HD double nextDouble() {
        return (double)nextBits(53) * 1.1102230246251565E-16;
    }

    XRPP_HD float nextFloat() {
        return (float)(nextBits(24) / (float)(1 << 24));
    }

    XRPP_HD Xoroshiro128PP createRandomDeriver() {
        uint64_t a = nextLong();
        uint64_t b = nextLong();
        return fromRaw(a, b);
    }

    XRPP_HD static uint64_t bytesToU64BE(const uint8_t* b) {
        uint64_t v = 0;
        for (int i = 0; i < 8; i++) v = (v << 8) | (uint64_t)b[i];
        return v;
    }

    XRPP_HD static Xoroshiro128PP createRandomFromDigest(const Xoroshiro128PP& deriver, const uint8_t md5digest[16]) {
        uint64_t l = bytesToU64BE(md5digest + 0);
        uint64_t m = bytesToU64BE(md5digest + 8);
        return fromRaw(l ^ deriver.loSeed, m ^ deriver.hiSeed);
    }

    Xoroshiro128PP createRandom(const std::string& name) const {
        uint8_t digest[16];
        md5String(name, digest);
        return createRandomFromDigest(*this, digest);
    }
};
