import unreal
sme = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
for a in unreal.EditorAssetLibrary.list_assets("/Game/Atlas/Geo", recursive=True):
    o = unreal.load_asset(a)
    if isinstance(o, unreal.StaticMesh):
        unreal.log(f"[Atlas] debug {o.get_name()} uv={sme.get_num_uv_channels(o, 0)} vc={sme.has_vertex_colors(o)} nanite={o.get_editor_property('nanite_settings').enabled}")
