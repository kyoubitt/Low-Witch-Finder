import net.minecraft.SharedConstants;
import net.minecraft.core.*;
import net.minecraft.core.registries.Registries;
import net.minecraft.data.registries.VanillaRegistries;
import net.minecraft.server.Bootstrap;
import net.minecraft.world.level.biome.*;
import net.minecraft.world.level.levelgen.*;
import net.minecraft.world.level.levelgen.densityfunction.SamplerContext;
import java.io.*;

/** 26.3, where the random state and the climate sampler changed shape. */
public class BiomeTruth {
    public static void main(String[] a) throws Exception {
        SharedConstants.tryDetectVersion();
        Bootstrap.bootStrap();
        HolderLookup.Provider p = VanillaRegistries.createWorldLookup();
        NoiseGeneratorSettings ngs = p.lookupOrThrow(Registries.NOISE_SETTINGS).getOrThrow(NoiseGeneratorSettings.OVERWORLD).value();
        Holder<MultiNoiseBiomeSourceParameterList> plh = (
            p.lookupOrThrow(Registries.MULTI_NOISE_BIOME_SOURCE_PARAMETER_LIST).getOrThrow(MultiNoiseBiomeSourceParameterLists.OVERWORLD));
        if (a.length > 1) { TD.dumpList(plh.value().parameters(), a[1]); return; }
        MultiNoiseBiomeSource src = MultiNoiseBiomeSource.createFromPreset(plh);
        BufferedReader in = new BufferedReader(new InputStreamReader(System.in));
        long cur = Long.MIN_VALUE; Climate.Sampler cs = null; String l;
        while ((l = in.readLine()) != null) {
            String[] t = l.trim().split(" ");
            long seed = Long.parseLong(t[0]);
            if (seed != cur || cs == null) { cs = RandomState.create(p.lookupOrThrow(Registries.NOISE), seed, ngs).createClimateSampler(SamplerContext.EMPTY_UNCACHED); cur = seed; }
            int x = Integer.parseInt(t[1]), y = Integer.parseInt(t[2]), z = Integer.parseInt(t[3]);
            Holder<Biome> b = src.getNoiseBiome(cs.sample(x >> 2, y >> 2, z >> 2));
            System.out.println("BIOME " + b.unwrapKey().get().identifier().getPath());
        }
    }
}
