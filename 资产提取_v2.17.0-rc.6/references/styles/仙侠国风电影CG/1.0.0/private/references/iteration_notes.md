# Distillation record

```yaml
macro_medium:
  primary_category: pure_3d_render
  confidence: 0.96
  evidence:
    skin: subsurface depth, age-localized lines, smooth render gradients, controlled studio response
    hair: coherent strand geometry and anisotropic highlight behavior
    fabric_or_materials: precise cloth, metal, stone, water, and ornament separation
    lighting_and_shadows: area-light modeling, contact shadows, global sky fill, atmospheric haze
    camera_depth_edges: render-like depth of field, clean asset edges, softened distance
  forbidden_defaults_if_wrong: [semi-real 2.5D digital painting, anime digital painting, painterly 2D illustration]
surface_language:
  visible_brushwork: low
  paper_or_canvas_grain: absent
  global_noise_or_speckle: absent
  texture_distribution: localized
  texture_evidence_regions: [age lines around eyes and forehead, hair strand groups, fabric borders, stone fracture planes]
  value_transition: smooth_gradients
  material_separation: precise
  highlight_design: render_like_precise
  likely_failure_if_misread: live-action portrait pasted into a hyper-sharp game environment
style_signal_split:
  transferable_invariants: [refined East Asian anatomy, age credibility, localized surface detail, construction-driven garment detail, broad material separation, cinematic atmosphere]
  conditional_style_variables: [age wrinkles, facial hair, makeup, long hair, ornate borders, night-blue environment, bright pearl costume]
  non_transferable_content: [named identities, exact faces, costumes, crowns, hats, jewelry, specific landscapes, architecture, compositions]
  missing_risk_if_only_pipeline_words: [age-specific bone and skin structure, quiet fabric panels, selective edge hierarchy, dry controlled highlights]
iterations:
  - iteration: 1
    scores: {face_closeup: 0.82, full_body: 0.86, environment: 0.84, object_closeup_in_environment: 0.79}
    overall_pass: false
    failures: face leaned photographic; environment and object carried excessive microdetail; object surfaces were too rough and glossy
    prompt_update: [make pure-3D construction explicit, localize skin detail, simplify environment masses, constrain wet gloss]
  - iteration: 2
    scores: {face_closeup: 0.92, full_body: 0.89, environment: 0.92, object_closeup_in_environment: 0.83}
    overall_pass: false
    failures: full-body ground and object ceramic remained too reflective; full-body age read younger than requested
    prompt_update: [dry matte-to-satin surfaces, no direct sun or tiny lights, stronger age anatomy, no mirror-like floor]
  - iteration: 3
    scores: {face_closeup: 0.94, full_body: 0.93, environment: 0.94, object_closeup_in_environment: 0.92}
    gates: {macro_medium: pass, style_fingerprint: pass, surface_language: pass, noise_overlay: pass, character_design: pass, material_response: pass, detail_distribution: pass, detail_integrity: pass, lighting_quality: pass, full_body_life: pass}
    overall_pass: true
materials:
  count: 16
  independent_images: true
  character_sensitive_anchors_regenerated: [skin, hair, fabric, leather]
  stable_environment_material_anchors_reused: [metal, glass, plastic, wood, stone, ceramic, paper, liquid, emissive, rubber, makeup, foliage]
  noise_overlay_gate: pass
  lighting_quality_gate: pass
```
