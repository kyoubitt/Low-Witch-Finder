// SwampHut region placement, ported from com.seedfinding.mcfeature.structure
// (UniformStructure.getInRegion + RegionStructure/OldStructure config) and
// com.seedfinding.mccore.rand.ChunkRand.setRegionSeed.
//
// [bytecode-verified] constants and formulas confirmed via javap -c against
// mc_feature-1.171.9.jar / mc_core-1.210.0.jar. The underlying RNG is the plain
// java.util.Random LCG (JRand), identical to the one already proven correct in the
// slime_toolchain project.
#pragma once
#include <cstdint>

#ifdef __CUDACC__
#define SH_HD __host__ __device__
#else
#define SH_HD
#endif

// ---- java.util.Random LCG ----
struct JavaRandom {
    uint64_t seed; // always kept masked to 48 bits

    SH_HD void setSeed(int64_t s) { seed = ((uint64_t)s ^ 0x5DEECE66DULL) & ((1ULL << 48) - 1); }

    SH_HD int32_t next(int bits) {
        seed = (seed * 0x5DEECE66DULL + 0xBULL) & ((1ULL << 48) - 1);
        return (int32_t)(int64_t)(seed >> (48 - bits));
    }

    // java.util.Random.nextInt(bound), including the power-of-two fast path and the
    // rejection loop for non-power-of-two bounds.
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

// [bytecode-verified] SwampHut.CONFIGS: salt is 14357617 for <1.13, 14357620 for >=1.13.
// spacing=32, separation=8 are the standard swamp hut region config (offset = 32-8 = 24).
struct SwampHutConfig {
    int spacing;
    int separation;
    int salt;

    SH_HD int offset() const { return spacing - separation; }
};

SH_HD inline SwampHutConfig swampHutConfigModern() { // 1.13+ (covers 1.18/1.19/1.20/1.21+)
    SwampHutConfig c;
    c.spacing = 32;
    c.separation = 8;
    c.salt = 14357620;
    return c;
}

SH_HD inline SwampHutConfig swampHutConfigLegacy() { // pre-1.13
    SwampHutConfig c;
    c.spacing = 32;
    c.separation = 8;
    c.salt = 14357617;
    return c;
}

// [bytecode-verified] ChunkRand.setRegionSeed:
//   regionSeed = regionX*341873128712 + regionZ*132897987541 + worldSeed + salt
// then setSeed(regionSeed).
SH_HD inline void setRegionSeed(JavaRandom& rand, int64_t worldSeed, int regionX, int regionZ, int salt) {
    int64_t regionSeed = (int64_t)regionX * 341873128712LL
                       + (int64_t)regionZ * 132897987541LL
                       + worldSeed
                       + (int64_t)salt;
    rand.setSeed(regionSeed);
}

// [bytecode-verified] UniformStructure.getInRegion:
//   setRegionSeed(...); return CPos(regionX*spacing + nextInt(offset),
//                                   regionZ*spacing + nextInt(offset))
// (result is in CHUNK coordinates; SearchCoords multiplies by 16 for block coords).
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

// SearchCoords.isHutRotated90 / checkHeight orientation test:
// structureSeed = seed & 0xFFFFFFFFFFFF; carverSeed(structureSeed, chunkX, chunkZ);
// first nextFloat() in [0,0.25) u [0.5,0.75) -> 7x9 footprint, else rotated 9x7.
// [bytecode-verified] ChunkRand.setCarverSeed: setSeed(worldSeed); then
//   seed = chunkX*nextLong() ^ chunkZ*nextLong() ^ worldSeed; setSeed(seed).
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
    int64_t structureSeed = worldSeed & 281474976710655LL; // (1<<48)-1
    JavaRandom rand;
    // NOTE: SearchCoords passes hutX/16, hutZ/16 -- Java int division truncates toward
    // zero, which differs from floorDiv for negatives. Reproduced exactly here.
    setCarverSeed(rand, structureSeed, hutBlockX / 16, hutBlockZ / 16);
    float a = javaNextFloat(rand);
    return !(a < 0.25f || (a >= 0.5f && a < 0.75f));
}
