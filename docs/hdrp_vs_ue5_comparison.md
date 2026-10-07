# Rendering stack for the vertical slice: URP-tiered vs HDRP vs Unreal Engine 5

**Item 8 of the Dream list.** A comparison and a recommendation, plus the research
behind it. Written for the vertical-slice prototype decision, and written to sit
under the **standing decision to prioritise URP tiered rendering over HDRP so
Android stays viable**.

**The recommendation, first:** build the vertical slice on **URP with a tiered
quality ladder**, and treat UE5 as the *visual-reference and optionally
high-end-target* path — not as the primary pipeline. HDRP is not recommended as
the project pipeline while Android is a target.

---

## 1. Why the decision is really about Android

The three options are not "worse / better / best". They are three different
answers to *which hardware must run this*:

| Stack | Runs on | Photoreal ceiling |
|---|---|---|
| **URP** (Universal RP) | Android/iOS, Switch, PC, console, XR — one codebase | Good; strong stylised. Not film-real-time |
| **HDRP** (High Definition RP) | PC + current-gen console | High; real-time ray tracing, volumetric lighting |
| **UE5** (Lumen + Nanite) | PC + current-gen console | Highest; the reference point for photoreal |

The constraint that decides it: **HDRP requires compute-shader-capable GPUs and,
for its ray-tracing features, DirectX 12 — and it does not support OpenGL ES
devices at all.** Most mobile GPUs meet neither requirement. So HDRP and Android
are not a tradeoff to weigh; they are mutually exclusive. Choosing HDRP is
choosing to drop Android.

UE5 is the same story from the other direction: Lumen and Nanite are built for
desktop/console GPU budgets, and a mobile target means shipping a materially
different renderer anyway.

Meanwhile URP is where Unity's investment is: **GPU Resident Drawer** and the
**SRP Batcher** exist specifically to cut CPU-side draw-call overhead, which is
the thing that actually caps frame rate on phones (mobile GPUs are typically
bandwidth-bound, not compute-bound), and **URP Deferred+** now supports a high
light count with smart culling and baking. In Unity 6, URP also picked up
materially better motion blur, depth of field, SSR and TAA on mobile-class
hardware — the features people used to reach for HDRP to get.

## 2. What a "tiered" URP ladder buys

One pipeline, one set of assets, several quality rungs selected at runtime:

| Tier | Target | Typical settings |
|---|---|---|
| **Low** | mid/low Android | forward, no SSR, baked lighting only, reduced shadow distance, 0.7–1.0 render scale |
| **Mid** | flagship Android, low-end PC | forward+, baked + a single real-time key light, limited post |
| **High** | PC | deferred+, SSR, SSAO, volumetrics from a URP-ported stack, full post |

The point is not that a tier looks as good as HDRP. It is that **the same
content ships on every target**, and quality becomes a setting rather than a
fork. That is the property that keeps Android viable without maintaining a
second project.

## 3. Where UE5 still wins, and what to do about it

Honestly: for pure photoreal fidelity, UE5 wins.

- **Nanite** virtualises geometry, so multi-million-poly photogrammetry and ZBrush
  meshes can be imported without retopology for visual-only assets. As of 5.7 it
  extends to foliage and skeletal meshes.
- **Lumen** gives fully dynamic global illumination — no bake cycle, so a lighting
  change is visible in minutes rather than after a rebake. For a slice whose whole
  purpose is look development, that iteration speed is a real advantage.

The practical way to use that without losing Android: **use UE5 as the
reference, not the runtime.** Render the look in UE5, then reproduce the lighting
and material *intent* in URP. Chasing pixel parity is the trap; matching
art direction and key light response is achievable and is what a vertical slice
needs to prove.

## 4. Cost and pipeline reality

- **Licensing:** Unreal is free until a product earns **$1M gross**, then a 5%
  royalty; a non-runtime seat is around $1,850/yr. Unity **Pro is ~$2,310 per
  seat per year**; the free **Personal** tier is capped at **$200,000** in revenue
  or funding. For a small project this is a real, if secondary, input.
- **Team gravity** is a first-class factor, not a soft one. The engine your senior
  people already have depth in is worth more than a feature-list advantage, and
  switching costs land on the critical path of a *vertical slice*, which is
  exactly where you can least afford them.
- **Asset pipeline culture** differs: Nanite-first (import dense, skip retopo)
  versus classical (author LODs, respect budgets). A URP tiered build is a
  classical-budget pipeline, and the art plan must say so up front.

## 5. Photorealistic techniques worth stealing into a URP build

None of these require HDRP or UE5, and each is the highest-value-per-effort item
for a slice that has to *read* as photoreal on a mid-range GPU:

1. **Physically-based lighting discipline over features.** Correct light units,
   believable exposure and a single strong key light with a bounced fill beats a
   stack of effects. Most "photoreal" failures are lighting-authoring failures.
2. **Baked GI for the hero, real-time for the accent.** Bake the environment,
   keep one dynamic source (sun or a practical) so the scene responds to time of
   day. This is what keeps the mobile tier affordable.
3. **Reflection probes + box projection.** Gets most of an SSR result for a
   fraction of the cost, and is what makes interiors stop looking flat.
4. **Post-processing as the last 20%.** ACES tonemapping, a well-authored bloom
   threshold, subtle chromatic aberration and film grain. Small, cheap, large
   perceived gain.
5. **Material micro-detail and vertex-blend layering.** Tiling macro maps blended
   with vertex-colour-driven detail masks is what removes the "tiled texture"
   read at no shading cost.
6. **Silhouette and density over resolution.** Hero assets get the triangles; the
   environment carries tiling materials and instancing. Mobile rewards this
   directly because it is draw-call- and bandwidth-bound.
7. **Decals and trim sheets** for set dressing instead of unique meshes — the
   single biggest texture-memory saving on a phone.

## 6. Recommendation

| Question | Answer |
|---|---|
| Primary pipeline for the vertical slice | **URP, tiered (Low / Mid / High)** |
| Reason | Keeps Android in the target set; one codebase, one asset set |
| Role of HDRP | **Not recommended** while Android is a target — it cannot run there |
| Role of UE5 | **Visual reference** for look development; possibly a separate high-end target later, not the runtime |
| What must be true for UE5-as-primary to be reconsidered | Android is dropped from the slice's target list, or the slice's goal becomes a fidelity benchmark rather than a shippable cross-platform slice |

This is consistent with the standing decision to prioritise URP tiered rendering
over HDRP, and it states the condition under which that decision would be worth
revisiting rather than leaving it as an assertion.

## Sources

- Unity URP/HDRP capability and platform constraints; mobile draw-call/bandwidth
  analysis and URP 6 improvements.
- UE5.7 Nanite (incl. foliage/skeletal) and Lumen dynamic GI; licensing terms.
- Unity licensing: Pro per-seat pricing and the Personal revenue/funding cap.
