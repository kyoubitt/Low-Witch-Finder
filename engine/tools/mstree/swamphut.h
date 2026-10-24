#pragma once
#include <cstdint>

#ifdef __CUDACC__
#define SH_HD __host__ __device__
#else
#define SH_HD
#endif

struct JavaRandom {
    uint64_t seed;

    SH_HD void setSeed(int64_t s) { seed = ((uint64_t)s ^ 0x5DEECE66DULL) & ((1ULL << 48) - 1); }

    SH_HD int32_t next(int bits) {
        seed = (seed * 0x5DEECE66DULL + 0xBULL) & ((1ULL << 48) - 1);
        return (int32_t)(int64_t)(seed >> (48 - bits));
    }

    SH_HD int32_t nextInt(int32_t bound) {
        if ((bound & -bound) == bound) {
            return (int32_t)(((int64_t)bound * (int64_t)next(31)) >> 31);
        }
        int32_t bits, val;
        do {
            bits = next(31);
            val = bits % bound;
        } while (bits - val + (bound - 1) < 0);
        return val;
    }
};

struct SwampHutConfig {
    int spacing;
    int separation;
    int salt;

    SH_HD int offset() const { return spacing - separation; }
};

SH_HD inline SwampHutConfig swampHutConfigModern() {
    SwampHutConfig c;
    c.spacing = 32;
    c.separation = 8;
    c.salt = 14357620;
    return c;
}

SH_HD inline SwampHutConfig swampHutConfigLegacy() {
    SwampHutConfig c;
    c.spacing = 32;
    c.separation = 8;
    c.salt = 14357617;
    return c;
}

SH_HD inline void setRegionSeed(JavaRandom& rand, int64_t worldSeed, int regionX, int regionZ, int salt) {
    int64_t regionSeed = (int64_t)regionX * 341873128712LL
                       + (int64_t)regionZ * 132897987541LL
                       + worldSeed
                       + (int64_t)salt;
    rand.setSeed(regionSeed);
}

struct ChunkPos { int x, z; };

SH_HD inline ChunkPos swampHutGetInRegion(int64_t worldSeed, int regionX, int regionZ,
                                          const SwampHutConfig& cfg) {
    JavaRandom rand;
    setRegionSeed(rand, worldSeed, regionX, regionZ, cfg.salt);
    ChunkPos p;
    p.x = regionX * cfg.spacing + rand.nextInt(cfg.offset());
    p.z = regionZ * cfg.spacing + rand.nextInt(cfg.offset());
    return p;
}

SH_HD inline int64_t javaNextLong(JavaRandom& rand) {
    int32_t hi = rand.next(32);
    int32_t lo = rand.next(32);
    return ((int64_t)hi << 32) + (int64_t)lo;
}

SH_HD inline void setCarverSeed(JavaRandom& rand, int64_t worldSeed, int chunkX, int chunkZ) {
    rand.setSeed(worldSeed);
    int64_t a = javaNextLong(rand);
    int64_t b = javaNextLong(rand);
    int64_t s = (int64_t)chunkX * a ^ (int64_t)chunkZ * b ^ worldSeed;
    rand.setSeed(s);
}

SH_HD inline float javaNextFloat(JavaRandom& rand) {
    return (float)rand.next(24) / (float)(1 << 24);
}

SH_HD inline bool isHutRotated90(int64_t worldSeed, int hutBlockX, int hutBlockZ) {
    int64_t structureSeed = worldSeed & 281474976710655LL;
    JavaRandom rand;

    setCarverSeed(rand, structureSeed, hutBlockX / 16, hutBlockZ / 16);
    float a = javaNextFloat(rand);
    return !(a < 0.25f || (a >= 0.5f && a < 0.75f));
}
