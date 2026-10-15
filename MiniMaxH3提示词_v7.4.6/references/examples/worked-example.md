# Worked Example — Official Ref2VA Assembly

This example follows `references/official/ref-en.txt`. The explanatory notes may be
Chinese, but the copy-ready prompt block is English except for the preserved Chinese
dialogue.

```text
subject_definitions:
<Subject 1> is the young swordswoman whose face, long black hair, white robe, and jade pendant come from <Picture 1>.
<Subject 2> is the misty mountain pass in <Picture 2>, with a stone bridge, one pine tree, and wet ground.
<Audio 1> is the voice-timbre reference for <Subject 1> (S1).

summary:
[reference generation + audio reference] The target video shows <Subject 1> crossing <Subject 2>, drawing her sword, and speaking one line while <Audio 1> guides her vocal timbre.

retention_analysis:
<Picture 1>: fully_preserved - the face, long black hair, white robe, and jade pendant are preserved as the source appearance for <Subject 1>.
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - the referenced facial identity, hair, robe, and jade pendant remain unchanged.
<Subject 2> (appears in [Shot 1], [Shot 2]): fully_preserved - the bridge, pine tree, mist, and wet ground remain consistent.
<Audio 1>: reference - its vocal timbre guides (S1) without copying the source signal.

detailed_description:
The target video uses a polished 3D CG Chinese-animation style with clean cel-influenced shading, refined fabric materials, controlled cinematic lighting, and stable character identity. It avoids photoreal live-action skin, flat 2D rendering, low-poly geometry, plastic toy surfaces, and mixed visual styles.
[Shot 1] A medium-wide shot establishes <Subject 2> at dawn. The stone bridge crosses the lower frame, one pine tree stands behind it, mist remains between the far rocks, and wet ground reflects a muted sky. <Subject 1> enters from frame right in the midground, preserving the appearance defined by <Picture 1>. She walks across the bridge with her right hand resting on the sword hilt. The camera tracks left at slow speed, keeping her full body and the bridge visible. Her robe hem moves with each step and the pendant settles against her chest after the second step.
[Shot 2] At 00:04.000, the shot cuts to a close shot from the same side of the axis. <Subject 1> (S1), an on-screen young woman with a clear low voice and measured delivery, tightens her fingers around the hilt and draws the blade halfway. Metal catches the pale light as the scabbard remains at her left hip. Using the timbre referenced from <Audio 1>, she says, <d>[Chinese] 山门之外，止步。</d> She closes her mouth, keeps her eyes fixed toward frame left, and completes the draw. The camera pushes in with small amplitude at slow speed until her face and guard fill the frame. The final image holds her planted feet, level shoulders, closed mouth, and sword at a stable diagonal while the mist continues drifting behind her.

overall_soundscape:
Light wind moves through the pine tree while water runs beneath the stone bridge. Wet footsteps, robe fabric, and one clean metallic draw remain audible under the spoken line.

non_diegetic_music:
Sparse low cello notes at a slow tempo with a restrained two-beat pulse, gradually decreasing in volume after the sword is drawn.
```

The example deliberately uses no `[Xs-Ys]` windows, Chinese field names, standalone
negative block, or invented reference labels.
