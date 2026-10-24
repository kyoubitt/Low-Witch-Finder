/* lysh biome.c —— 见 biome.h 的取证说明。
 *
 * 判定 = MC 的 `MultiNoiseBiomeSource.getNoiseBiome`：
 *   ① 在 quart 位置求 6 个气候密度函数 → 量化成 TargetPoint；
 *   ② 在参数表上做最近点搜索（`Climate.ParameterList.findValue` → `Climate.RTree.search`）。
 *
 * ②用的是游戏自己的 R 树，逐节点导出（biome_trees.h，由 tools/accuracy/gen_trees.py 从各版本的游戏代码生成），
 *   搜索与 `Climate.RTree.SubTree.search` 逐行一致，所以并列时的取舍也与游戏相同。
 *   每个版本用它自己的树（1.18 / 1.19 / 1.19.3 / 1.20 / 1.21.4 / 1.21.5 / 26.2 / 26.3 共 8 棵）。
 */
#include "biome.h"

#include "column_top.h"
#include "mcmath.h"

/* ---- the biome search tree: the game's own, walked the way the game walks it ---- */
typedef struct lysh_gtree {
    const int16_t (*range)[12];     /* per node: 6 (min, max) parameter ranges */
    const uint8_t *kids, *biome;    /* children count (0 = leaf), biome id (leaf) */
    const uint16_t *end;            /* index just past the node's subtree */
} lysh_gtree;

static int64_t gt_dist(const lysh_gtree *g, int i, const int64_t np[6]) {
    int64_t ds = 0;
    for (int k = 0; k < 6; k++) {
        int64_t d = np[k] - g->range[i][2 * k + 1], e = (int64_t)g->range[i][2 * k] - np[k];
        d = d > 0 ? d : (e > 0 ? e : 0);
        ds += d * d;
    }
    return ds;
}

/* Climate.RTree.SubTree.search, line for line */
static int gt_search(const lysh_gtree *g, int node, const int64_t np[6], int leaf) {
    if (g->kids[node] == 0) return node;
    int64_t best = leaf < 0 ? INT64_MAX : gt_dist(g, leaf, np);
    int result = leaf, c = node + 1;
    for (int k = 0; k < g->kids[node]; k++, c = g->end[c]) {
        int64_t d = gt_dist(g, c, np);
        if (best > d) {
            int l2 = gt_search(g, c, np, result);
            int64_t d2 = (l2 == c) ? d : gt_dist(g, l2, np);
            if (best > d2) { best = d2; result = l2; }
        }
    }
    return result;
}

static int tree_biome(const lysh_gtree *g, const uint64_t np[6]) {
    int64_t s[6];
    for (int i = 0; i < 6; i++) s[i] = (int64_t)np[i];
    return g->biome[gt_search(g, 0, s, -1)];
}

#include "biome_trees.h"
static const lysh_gtree LYSH_TREES[LYSH_TREE_COUNT] = LYSH_TREE_REFS;

int lysh_biome_version(const char *name, int *tree, int *is_118, int *pre_262) {
    for (unsigned i = 0; i < sizeof lysh_versions / sizeof lysh_versions[0]; i++)
        if (!strcmp(lysh_versions[i].name, name)) {
            if (tree) *tree = lysh_versions[i].tree;
            if (is_118) *is_118 = lysh_versions[i].is_118;
            if (pre_262) *pre_262 = lysh_versions[i].pre_262;
            return 0;
        }
    return 1;
}

/* Climate.quantizeCoord(float) = (long)(f * 10000.0f)
 * ⚠️ 乘法必须在 **float** 里做（先转 double 再乘会差 1）。 */
static inline long long quantize(float f) {
    return (long long)(f * 10000.0f);
}

/* DensityFunctions.yClampedGradient(-64 -> 320, 1.5 -> -1.5)，`overworld/depth` 的
 * argument1（argument2 是 `overworld/offset`）。与 column_top.c 的 ygrad 同一口径。 */
static inline double depth_gradient(double y) {
    double t = mc_clamp((y - -64.0) / (320.0 - -64.0), 0.0, 1.0);
    return mc_lerp(t, 1.5, -1.5);
}

void lysh_biome_init(lysh_biome *b, uint64_t world_seed, int large_biomes, int tree_sel) {
    lysh_dblnoise_from_seed_id(&b->temperature, world_seed,
                               large_biomes ? "minecraft:temperature_large"
                                            : "minecraft:temperature");
    lysh_dblnoise_from_seed_id(&b->vegetation, world_seed,
                               large_biomes ? "minecraft:vegetation_large"
                                            : "minecraft:vegetation");
    b->tree = &LYSH_TREES[tree_sel >= 0 && tree_sel < LYSH_TREE_COUNT ? tree_sel : 0];
}

const char *lysh_biome_data_version(void) { return "the game's own trees, 1.18 to 26.3"; }

int lysh_swamp_hut_biome_ok(const lysh_biome *b, const lysh_terrain *t,
                            int hut_x, int hut_z,
                            lysh_biome_height_fn height_fn, void *height_user,
                            lysh_biome_result *out) {
    if (out) {
        for (int i = 0; i < 6; i++) out->target.t[i] = 0;
        out->target.qx = out->target.qy = out->target.qz = 0;
        out->target.occ_y = 0;
        out->winner = -1;
        out->ok = 0;
    }
    if (!out || !height_fn || !b->tree) return 0;

    /* ---- Structure.onTopOfChunkCenter ---- */
    int chunk_x = hut_x >> 4;
    int chunk_z = hut_z >> 4;
    int mid_x = chunk_x * 16 + 8;          /* ChunkPos.getMiddleBlockX */
    int mid_z = chunk_z * 16 + 8;

    /* getFirstOccupiedHeight(...) = getBaseHeight(WORLD_SURFACE_WG) - 1 */
    int y = height_fn(height_user, mid_x, mid_z) - 1;

    /* ---- QuartPos.fromBlock ---- */
    int qx = mid_x >> 2;
    int qy = y >> 2;
    int qz = mid_z >> 2;

    /* ---- Climate.Sampler.sample：六个密度函数在 quart 位置求值 ---- */
    /* ShiftedNoise 的 xz_scale 是 0.25、y_scale 是 0.0，且这些噪声共用同一对 shift；
     * lysh_terrain_info_at 吃的就是 biome(quart) 坐标，shifted_x/shifted_z 已算好，
     * 与密度函数路径逐位等价（见 column_top.h 的实测）。*/
    lysh_terrain_info ti;
    lysh_terrain_info_at(t, qx, qz, &ti);

    double temperature = lysh_dblnoise_sample_y0(&b->temperature, ti.shifted_x, ti.shifted_z);
    double humidity    = lysh_dblnoise_sample_y0(&b->vegetation,  ti.shifted_x, ti.shifted_z);

    /* depth = y_clamped_gradient(y) + overworld/offset（都是 double 语义）。
     * 方块 y 用 QuartPos.toBlock(qy) = qy*4。
     * ⚠️ LARGE_BIOMES 用的是 `overworld_large_biomes/offset`（与 NORMAL 不同的样条），
     *    这里复用的是 NORMAL 的那一条 ⇒ LARGE_BIOMES 下 depth 这一维是近似值。
     *    默认的 NORMAL 世界不受影响。 */
    double depth = depth_gradient((double)(qy * 4)) + lysh_overworld_offset(t, qx * 4, qz * 4);

    uint64_t np[6];
    np[0] = (uint64_t)quantize((float)temperature);
    np[1] = (uint64_t)quantize((float)humidity);
    np[2] = (uint64_t)quantize((float)ti.continentalness);
    np[3] = (uint64_t)quantize((float)ti.erosion);
    np[4] = (uint64_t)quantize((float)depth);
    np[5] = (uint64_t)quantize((float)ti.weirdness);

    int winner = tree_biome((const lysh_gtree *)b->tree, np);

    for (int a = 0; a < 6; a++) out->target.t[a] = (long long)np[a];
    out->target.qx = qx;
    out->target.qy = qy;
    out->target.qz = qz;
    out->target.occ_y = y;
    out->winner = winner;
    out->ok = (winner == LYSH_BIOME_ID_SWAMP) ? 1 : 0;
    return out->ok;
}
