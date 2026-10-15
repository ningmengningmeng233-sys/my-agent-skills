# Reference router

```yaml
subject_routes:
  face: [face.png, face_base_style.md]
  full_body: [full_body.png, full_body_base_style.md]
  environment: [environment.png, environment_base_style.md]
  object: [object.png, object_base_style.md]
  mixed: read only subject routes actually visible
material_routes:
  skin: [materials/skin.png, materials/skin_base_style.md]
  hair: [materials/hair.png, materials/hair_base_style.md]
  fabric: [materials/fabric.png, materials/fabric_base_style.md]
  leather: [materials/leather.png, materials/leather_base_style.md]
  metal: [materials/metal.png, materials/metal_base_style.md]
  glass: [materials/glass.png, materials/glass_base_style.md]
  plastic: [materials/plastic.png, materials/plastic_base_style.md]
  wood: [materials/wood.png, materials/wood_base_style.md]
  stone: [materials/stone.png, materials/stone_base_style.md]
  ceramic: [materials/ceramic.png, materials/ceramic_base_style.md]
  paper: [materials/paper.png, materials/paper_base_style.md]
  liquid: [materials/liquid.png, materials/liquid_base_style.md]
  emissive: [materials/emissive.png, materials/emissive_base_style.md]
  rubber: [materials/rubber.png, materials/rubber_base_style.md]
  makeup: [materials/makeup.png, materials/makeup_base_style.md]
  foliage: [materials/foliage.png, materials/foliage_base_style.md]
```

A portrait reads `face + skin + hair`, adding makeup only if visible. A full figure reads `full_body` plus actual body and wardrobe materials. An empty place never reads character files. An object never reads character files. Examples: metal hair ornament = `object + metal`; stone temple = `environment + stone`; makeup portrait = `face + skin + hair + makeup`; clothed action figure = `full_body + skin + hair + fabric` plus visible leather or metal.
