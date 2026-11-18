// Vue 3D de la scène HQ : la même scène que les rendus Cycles (géométrie exportée de Blender par pipeline/web_export.py),
// mêmes matières, même ciel, même soleil, mêmes points de vue. Temps réel : pas de lancer de rayons, donc ombres
// par carte d'ombre, lumière du ciel par l'HDRI, occlusion ambiante en espace écran.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { HDRLoader } from "three/addons/loaders/HDRLoader.js";
import { GTAOPass } from "three/addons/postprocessing/GTAOPass.js";
import { FullScreenQuad } from "three/addons/postprocessing/Pass.js";

// brume linéaire (comme la passe Mist de Blender), pas le smoothstep de three
THREE.ShaderChunk.fog_fragment = `
#ifdef USE_FOG
	float fogFactor = clamp( ( vFogDepth - fogNear ) / ( fogFar - fogNear ), 0.0, 1.0 );
	gl_FragColor.rgb = mix( gl_FragColor.rgb, fogColor, fogFactor );
#endif`;

const GLSL = `
uniform sampler2D uNoise;
float luma_( vec3 c ) { return dot( c, vec3( 0.2126, 0.7152, 0.0722 ) ); }
float hash21( vec2 p ) { p = fract( p * vec2( 123.34, 456.21 ) ); p += dot( p, p + 45.32 ); return fract( p.x * p.y ); }
float grain_( vec3 c, float ref, float lo, float hi ) { return clamp( luma_( c ) / ref, lo, hi ); }
`;

/** Greffe du code dans un matériau standard de three (couleur, rugosité, émission, sommets). */
function patch(mat, o) {
  mat.onBeforeCompile = sh => {
    Object.assign(sh.uniforms, o.uniforms || {});
    let v = sh.vertexShader, f = sh.fragmentShader;
    v = v.replace("#include <common>", "#include <common>\nvarying vec3 vWP;\n" + (o.vpars || ""));
    if (o.vnormal) v = v.replace("#include <beginnormal_vertex>", o.vnormal);
    if (o.vbegin) v = v.replace("#include <begin_vertex>", o.vbegin);
    v = v.replace("#include <project_vertex>", "#include <project_vertex>\n#ifdef USE_INSTANCING\nvWP = ( modelMatrix * instanceMatrix * vec4( transformed, 1.0 ) ).xyz;\n#else\nvWP = ( modelMatrix * vec4( transformed, 1.0 ) ).xyz;\n#endif\n" + (o.vmain || ""));
    if (o.vshadow) v = v.replace("#include <shadowmap_vertex>", o.vshadow + "\n#include <shadowmap_vertex>");
    f = f.replace("#include <common>", "#include <common>\nvarying vec3 vWP;\n" + GLSL + (o.fpars || ""));
    if (o.fcolor) f = f.replace("#include <color_fragment>", "#include <color_fragment>\n" + o.fcolor);
    if (o.frough) f = f.replace("#include <roughnessmap_fragment>", "#include <roughnessmap_fragment>\n" + o.frough);
    if (o.fmetal) f = f.replace("#include <metalnessmap_fragment>", "#include <metalnessmap_fragment>\n" + o.fmetal);
    if (o.femis) f = f.replace("#include <emissivemap_fragment>", "#include <emissivemap_fragment>\n" + o.femis);
    if (o.fphys) f = f.replace("#include <lights_physical_fragment>", "#include <lights_physical_fragment>\n" + o.fphys);
    if (o.fnormal) f = f.replace("#include <normal_fragment_maps>", "#include <normal_fragment_maps>\n" + o.fnormal);
    if (o.occ && OCC.tOcc.value) {
      // lumière du ciel atténuée dans les rues encaissées (part de ciel visible calculée sur le relief LiDAR)
      Object.assign(sh.uniforms, OCC);
      f = f.replace("#include <common>", "#include <common>\nuniform sampler2D tOcc; uniform vec4 uOcc;")
        .replace("#include <aomap_fragment>", `{
          vec2 ouv = vec2( vWP.x, - vWP.z ) / uOcc.x;
          if ( ouv.x > 0.0 && ouv.x < 1.0 && ouv.y > 0.0 && ouv.y < 1.0 ) {
            vec3 ot = texture2D( tOcc, ouv ).rgb;
            float ogz = uOcc.y + ot.g * uOcc.z;
            float oth = clamp( ( vWP.y - ogz ) / max( ot.b * uOcc.w, 2.5 ), 0.0, 1.0 );
            float occ = mix( clamp( ot.r * ${o.occ.toFixed(2)}, 0.0, 1.0 ), 1.0, oth * oth );
            reflectedLight.indirectDiffuse *= occ; reflectedLight.indirectSpecular *= mix( 1.0, occ, 0.75 );
          } }\n#include <aomap_fragment>`);
    }
    sh.vertexShader = v; sh.fragmentShader = f;
  };
  mat.customProgramCacheKey = () => o.key;
  return mat;
}

const OCC = { tOcc: { value: null }, uOcc: { value: new THREE.Vector4(1000, 0, 1, 60) } };
const BLD_V = `attribute vec4 aCol; varying vec3 vTint; varying float vSeed; varying vec2 vUv0;`;
const BLD_VM = `vTint = pow( aCol.rgb, vec3( 2.2 ) ); vSeed = aCol.a * 2.0; vUv0 = uv;`;
const BLD_F = `varying vec3 vTint; varying float vSeed; varying vec2 vUv0;`;

export class HQViewer {
  constructor(el, { onStatus } = {}) {
    this.el = el; this.onStatus = onStatus || (() => {});
    const r = this.renderer = new THREE.WebGLRenderer({ antialias: false, powerPreference: "high-performance", reversedDepthBuffer: true })   // profondeur inversée : précise jusqu'à l'horizon;
    r.setPixelRatio(Math.min(devicePixelRatio, 2));
    r.outputColorSpace = THREE.SRGBColorSpace;
    r.toneMapping = THREE.NoToneMapping;
    r.shadowMap.enabled = true;
    r.shadowMap.type = THREE.PCFShadowMap;
    r.domElement.style.cssText = "position:absolute;inset:0;width:100%;height:100%;display:block";
    el.appendChild(r.domElement);
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(40, 1, 0.5, 40000);
    this.controls = new OrbitControls(this.camera, r.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.12;
    this.controls.maxPolarAngle = Math.PI / 2 - 0.01;
    this.controls.zoomToCursor = true;
    this.sun = new THREE.DirectionalLight(0xffffff, 8);
    this.sun.castShadow = true;
    this.sun.shadow.mapSize.set(4096, 4096);
    // lumière renvoyée par le sol et les façades au soleil (Cycles la calcule, pas le temps réel)
    this.bounce = new THREE.HemisphereLight(0x000000, 0xffffff, 1);
    this.scene.add(this.sun, this.sun.target, this.bounce);
    this.fog = this.scene.fog = new THREE.Fog(0x9cc3dc, 700, 30000);
    this.uSun = { value: new THREE.Vector3(0, 1, 0) };
    this.uTime = { value: 0 };
    this.lods = []; this.hdr = {}; this.keys = new Set();
    this.hfov = 55; this.ao = true; this.shot = null; this.root = null;
    // rendu : cible HDR multi-échantillonnée → occlusion ambiante → AgX + contraste
    this.rt = new THREE.WebGLRenderTarget(4, 4, { type: THREE.HalfFloatType, samples: 4, depthTexture: new THREE.DepthTexture(4, 4, THREE.FloatType) });
    this.rtAO = new THREE.WebGLRenderTarget(4, 4, { type: THREE.HalfFloatType });
    this.gtao = new GTAOPass(this.scene, this.camera, 4, 4, { depthTexture: this.rt.depthTexture },
      { radius: 2.2, distanceExponent: 1.4, thickness: 1.6, scale: 1.15, samples: 12, distanceFallOff: 1.0, screenSpaceRadius: false });
    this.final = new FullScreenQuad(new THREE.ShaderMaterial({
      uniforms: { tDiffuse: { value: null }, toneMappingExposure: { value: 1 }, uContrast: { value: 0.45 }, uSat: { value: 1.06 } },
      vertexShader: `varying vec2 vUv; void main() { vUv = uv; gl_Position = vec4( position.xy, 0.0, 1.0 ); }`,
      fragmentShader: `
        uniform sampler2D tDiffuse; uniform float uContrast; uniform float uSat; varying vec2 vUv;
        #include <common>
        #include <tonemapping_pars_fragment>
        void main() {
          vec3 c = AgXToneMapping( texture2D( tDiffuse, vUv ).rgb );
          c = sRGBTransferOETF( vec4( c, 1.0 ) ).rgb;
          c = c - uContrast * c * ( 1.0 - c ) * ( 1.0 - c );             // look « contraste moyen-fort » de Blender : ombres plus denses, hautes lumières intactes
          c = mix( vec3( dot( c, vec3( 0.2126, 0.7152, 0.0722 ) ) ), c, uSat );
          gl_FragColor = vec4( clamp( c, 0.0, 1.0 ), 1.0 );
        }`,
      depthTest: false, depthWrite: false,
    }));
    new ResizeObserver(() => this.resize()).observe(el);
    addEventListener("keydown", e => { if (this.active && !/INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) this.keys.add(e.code); });
    addEventListener("keyup", e => this.keys.delete(e.code));
    this.clock = new THREE.Clock();
    this.active = false; this.frames = 0; this.fps = 0; this._t = performance.now();
    const loop = () => { requestAnimationFrame(loop); if (this.active && this.root) this.render(); };
    loop();
  }

  resize() {
    const w = this.el.clientWidth, h = this.el.clientHeight;
    if (!w || !h) return;
    this.renderer.setSize(w, h, false);
    const pr = this.renderer.getPixelRatio(), W = Math.round(w * pr), H = Math.round(h * pr);
    this.rt.setSize(W, H); this.rtAO.setSize(W, H); this.gtao.setSize(W, H);
    this.camera.aspect = w / h;
    this.camera.fov = THREE.MathUtils.radToDeg(2 * Math.atan(Math.tan(THREE.MathUtils.degToRad(this.hfov) / 2) / Math.max(this.camera.aspect, 1)));
    this.camera.updateProjectionMatrix();
  }

  // ------------------------------------------------------------------ chargement
  async load(base, version) {
    this.dispose();
    const q = `?t=${version}`;
    const S = this.S = await (await fetch(`${base}scene.json${q}`)).json();
    this.base = base; this.q = q;
    const root = new THREE.Group();
    const mgr = this.mgr = new THREE.LoadingManager();
    const TL = new THREE.TextureLoader(mgr), GL = new GLTFLoader(mgr);
    const aniso = this.renderer.capabilities.getMaxAnisotropy();
    const tex = (name, srgb = true, repeat = true) => {
      const t = TL.load(`${base}tex/${name}${q}`);
      t.colorSpace = srgb ? THREE.SRGBColorSpace : THREE.NoColorSpace;
      if (repeat) t.wrapS = t.wrapT = THREE.RepeatWrapping;
      t.anisotropy = aniso;
      return t;
    };
    const uNoise = this.uNoise = { value: tex("noise.png", false) };
    const pending = this.pend = [];
    const U = o => ({ uNoise, ...o });
    this.onStatus("Chargement de la scène…");
    OCC.tOcc.value = null;
    try {
      const o = await (await fetch(`${base}tex/skyocc.json${q}`)).json();
      OCC.tOcc.value = tex("skyocc.png", false, false); OCC.uOcc.value.set(S.size, o.zmin, o.zrange, o.hmax);
    } catch { /* pas de carte : éclairage du ciel uniforme */ }

    // ---- sol du secteur : matières par masque (chaussée, trottoir rouge, pavé, sable, jardins) sur fond d'orthophoto
    const F = S.fields || {};
    const ground = patch(new THREE.MeshStandardMaterial({ roughness: 0.9, normalMap: tex("asphalt_02_nor.jpg", false), normalScale: new THREE.Vector2(0.5, 0.5) }), {
      key: "sol", occ: 1.3,
      uniforms: U({ tOrtho: { value: tex("ground_ortho.jpg", true, false) }, tMa: { value: tex("ground_mask_a.png", false, false) }, tMb: { value: tex("ground_mask_b.png", false, false) },
        tAsph: { value: tex("asphalt_02_diff.jpg") }, tCour: { value: tex("gravel_concrete_diff.jpg") }, tPave: { value: tex("patterned_paving_diff.jpg") },
        tSand: { value: tex("coast_sand_01_diff.jpg") }, tGrass: { value: tex("leafy_grass_diff.jpg") }, tPaveN: { value: tex("patterned_paving_nor.jpg", false) },
        uSize: { value: S.size }, uWater: { value: S.water }, uWalk: { value: new THREE.Vector3(...(S.walk_rgb || [0.2, 0.07, 0.06])) } }),
      vmain: `#ifdef USE_NORMALMAP\n vNormalMapUv = vec2( vWP.x, - vWP.z ) * 0.25;\n#endif`,
      fpars: `uniform sampler2D tOrtho, tMa, tMb, tAsph, tCour, tPave, tSand, tGrass, tPaveN; uniform float uSize, uWater; uniform vec3 uWalk; float gRough; float gPave; vec3 gPn;`,
      fcolor: `
        vec2 P = vec2( vWP.x, - vWP.z );
        vec2 guv = P / uSize;
        vec3 og = texture2D( tOrtho, guv ).rgb; og = mix( vec3( luma_( og ) ), og, 0.85 );
        vec4 ma = texture2D( tMa, guv ), mb = texture2D( tMb, guv );
        float road = ma.r, walk = ma.g, sand = ma.b, grass = mb.r, pave = mb.g, mark = mb.b;
        float big = texture2D( uNoise, P / 70.0 ).r;
        float wearf = 0.82 + big * 0.36;
        vec3 asphT = texture2D( tAsph, P / 4.0 ).rgb;
        vec3 c = mix( grain_( texture2D( tCour, P / 3.0 ).rgb, 0.30, 0.6, 1.5 ) * vec3( 0.42, 0.40, 0.37 ), og, 0.45 );
        c = mix( c, mix( texture2D( tGrass, P / 2.5 ).rgb * vec3( 0.9, 1.0, 0.8 ), og, 0.35 ), grass );
        vec3 asph = grain_( asphT, 0.20, 0.6, 1.5 ) * vec3( 0.115, 0.115, 0.12 ) * wearf;
        float repr = step( 0.86, hash21( floor( P / 11.0 + texture2D( uNoise, P / 40.0 ).gb * 1.5 ) ) );
        asph = mix( asph, vec3( 0.06, 0.06, 0.065 ), repr * 0.35 );
        c = mix( c, mix( asph, og, 0.12 ), road );
        c = mix( c, grain_( asphT, 0.20, 0.7, 1.35 ) * uWalk * wearf, walk );
        vec3 pv = grain_( texture2D( tPave, P / 2.2 ).rgb, 0.22, 0.55, 1.5 ) * vec3( 0.40, 0.37, 0.33 ) * wearf;
        float sf = clamp( ( texture2D( uNoise, P / 9.0 ).g - 0.5 ) * 2.2, 0.0, 1.0 );
        pv = mix( pv, vec3( 0.10, 0.095, 0.085 ), sf * 0.45 );
        gPn = texture2D( tPaveN, P / 2.2 ).xyz * 2.0 - 1.0; gPave = pave;
        pv *= mix( 0.62, 1.0, smoothstep( 0.80, 0.985, gPn.z ) );      // joints du pavage
        c = mix( c, mix( pv, og, 0.08 ), pave );
        c = mix( c, mix( texture2D( tSand, P / 3.0 ).rgb, og, 0.5 ) * vec3( 0.90, 0.80, 0.66 ), sand );
        float wet = sand * clamp( 1.0 - max( vWP.y - uWater, 0.0 ) / 1.1, 0.0, 1.0 );
        c = mix( c, vec3( 0.22, 0.17, 0.12 ), wet * 0.55 );
        mark = clamp( mark * ( 0.55 + texture2D( uNoise, P / 2.5 ).b * 0.5 ), 0.0, 1.0 );
        c = mix( c, vec3( 0.82, 0.82, 0.80 ), mark );
        diffuseColor.rgb = c;
        gRough = mix( mix( mix( mix( 0.92, 0.8, road ), 0.95, sand ), 0.22, wet ), 0.6, mark );`,
      frough: `roughnessFactor = gRough;`,
      fnormal: `normal = normalize( normal + gPave * 1.3 * ( viewMatrix * vec4( gPn.x, 0.0, - gPn.y, 0.0 ) ).xyz );`,
    });
    const drape = (image, o, sat, val, key) => patch(new THREE.MeshStandardMaterial({ roughness: 0.95, flatShading: true }), {
      key, uniforms: U({ tOrtho: { value: tex(image, true, false) }, uO: { value: new THREE.Vector3(o.ox, o.oy, o.span) }, uSV: { value: new THREE.Vector2(sat, val) } }),
      fpars: `uniform sampler2D tOrtho; uniform vec3 uO; uniform vec2 uSV;`,
      fcolor: `vec3 oc = texture2D( tOrtho, ( vec2( vWP.x, - vWP.z ) - uO.xy ) / uO.z ).rgb;
               diffuseColor.rgb = mix( vec3( luma_( oc ) ), oc, uSV.x ) * uSV.y;`,
    });
    const fields = [];
    const sz = S.size;
    if (F.terrain) fields.push(this.field(F.terrain, { material: ground, smooth: true, steps: [1, 2, 4], dists: [0, 500, 1300] }));
    if (F.abords && S.near) fields.push(this.field(F.abords, { material: drape("near_ortho.jpg", S.near, 0.95, 0.92, "abords"), steps: [1, 2, 4], dists: [0, 380, 950], hole: [3, 3, sz - 3, sz - 3] }));
    if (F.couronne && S.mid) fields.push(this.field(F.couronne, { material: drape("mid_ortho.jpg", S.mid, 0.95, 0.92, "couronne"), steps: [1, 2, 4], dists: [0, 900, 2200],
      hole: S.near ? [S.near.ox + 6, S.near.oy + 6, S.near.ox + S.near.span - 6, S.near.oy + S.near.span - 6] : null }));
    if (F.contexte) fields.push(this.field(F.contexte, { material: drape("ctx_ortho.jpg", S.ctx, 0.92, 0.95, "contexte"), smooth: true, steps: [1], dists: [0],
      hole: S.mid ? [S.mid.ox + 40, S.mid.oy + 40, S.mid.ox + S.mid.span - 40, S.mid.oy + S.mid.span - 40] : null }));

    // ---- eau du Bassin : teinte de l'orthophoto, surface réfléchissante ridée par le clapot
    const water = new THREE.Mesh(new THREE.PlaneGeometry(160000, 160000).rotateX(-Math.PI / 2), patch(new THREE.MeshStandardMaterial({ roughness: 0.07, metalness: 0, envMapIntensity: 0.62 }), {
      key: "eau", uniforms: U({ tOrtho: { value: tex("ctx_ortho.jpg", true, false) }, tWn: { value: tex("water_n.png", false) }, uO: { value: new THREE.Vector3(S.ctx.ox, S.ctx.oy, S.ctx.span) }, uTime: this.uTime }),
      fpars: `uniform sampler2D tOrtho, tWn; uniform vec3 uO; uniform float uTime;`,
      fcolor: `
        vec2 P = vec2( vWP.x, - vWP.z ); vec2 q = ( P - uO.xy ) / uO.z;
        vec3 deep = vec3( 0.035, 0.085, 0.085 );
        vec3 oc = texture2D( tOrtho, clamp( q, 0.0, 1.0 ) ).rgb;
        vec3 w = mix( vec3( luma_( oc ) ), oc, 1.05 ) * 0.80;
        w = mix( w, vec3( 0.07, 0.13, 0.13 ), clamp( ( luma_( oc ) - 0.16 ) / 0.10, 0.0, 1.0 ) );
        float edge = clamp( ( max( abs( q.x - 0.5 ), abs( q.y - 0.5 ) ) - 0.40 ) / 0.10, 0.0, 1.0 );
        diffuseColor.rgb = mix( w, deep, edge );`,
      fnormal: `
        vec2 Pw = vec2( vWP.x * 0.55, - vWP.z );
        vec2 wv = uTime * vec2( 0.035, 0.012 );
        vec3 n1 = texture2D( tWn, Pw / 9.0 + wv ).xyz * 2.0 - 1.0;
        vec3 n2 = texture2D( tWn, Pw.yx / 2.3 - wv * 1.7 ).xyz * 2.0 - 1.0;
        float gust = 0.35 + 1.3 * texture2D( uNoise, vec2( vWP.x, - vWP.z ) / 220.0 ).r;
        float far = 1.0 / ( 1.0 + length( vViewPosition ) / 600.0 );
        vec2 d = ( n1.xy + n2.xy * 0.45 ) * 0.10 * gust * far;
        normal = normalize( ( viewMatrix * vec4( normalize( vec3( d.x, 1.0, - d.y ) ), 0.0 ) ).xyz );`,
    }));
    water.position.set(sz / 2, S.water, -sz / 2);
    water.receiveShadow = true;
    root.add(water, ...fields);

    // ---- bâtiments : un matériau par nom, la teinte de chaque face est dans la couleur de sommet
    const bU = o => U(o);
    const wall = (name, useTint, scale) => {
      const m = patch(new THREE.MeshStandardMaterial({ flatShading: true, side: THREE.DoubleSide, roughness: 0.85,
        normalMap: tex(`${name}_nor.jpg`, false), normalScale: new THREE.Vector2(0.6, 0.6), roughnessMap: tex(`${name}_rough.jpg`, false) }), {
        key: "mur" + name, occ: 1.8, uniforms: bU({ tTex: { value: tex(`${name}_diff.jpg`) }, uScale: { value: scale } }),
        vpars: BLD_V + `attribute vec2 aUv1; attribute vec4 aTrim; varying vec2 vUv1; varying vec4 vTrim;`,
        vmain: BLD_VM + `vUv1 = aUv1; vTrim = vec4( pow( aTrim.rgb, vec3( 2.2 ) ), aTrim.a );`,
        fpars: BLD_F + `varying vec2 vUv1; varying vec4 vTrim; uniform sampler2D tTex; uniform float uScale;`,
        fcolor: `
          vec3 tx = texture2D( tTex, vUv0 * uScale ).rgb;
          vec3 base = ${useTint ? "vTint * ( luma_( tx ) * 0.35 + 0.66 )" : "tx"};
          float u = vUv0.x, v = vUv0.y, seglen = vUv1.x, walltop = vUv1.y;
          float dirt = ( 1.0 - min( v / 1.2, 1.0 ) ) * 0.35 + ( 0.5 + ( texture2D( uNoise, vUv0 / 12.0 ).r - 0.5 ) * 0.5 ) * 0.12;
          base = mix( base, vec3( 0.18, 0.16, 0.14 ), dirt );
          float fromTop = walltop - v;
          float under = clamp( 1.0 - fromTop / 2.5, 0.0, 1.0 );
          float st = clamp( ( ( texture2D( uNoise, vec2( u * 0.7, v * 0.03 ) ).g - 0.5 ) * 0.5 - 0.02 ) * 3.0, 0.0, 1.0 );
          st *= ( 0.25 + 0.75 * under ) * ( 0.25 + fract( vSeed * 13.37 ) * 0.75 ) * 0.38;
          base = mix( base, vec3( 0.10, 0.095, 0.085 ), st );
          float bl = ( texture2D( uNoise, vUv0 / 5.0 ).a - 0.5 ) * 0.5 * 0.22;
          base = mix( base, bl > 0.0 ? vec3( 0.95, 0.93, 0.88 ) : vec3( 0.05 ), abs( bl ) );
          base = mix( base, vec3( 0.08, 0.075, 0.07 ), clamp( 1.0 - fromTop / 0.35, 0.0, 1.0 ) * 0.22 );
          float harp = fract( v / 0.5 ) < 0.5 ? 0.55 : 0.35;
          float corner = step( seglen * 0.5 - harp, abs( u ) ) * step( v, walltop - 0.05 ) * step( 1.2, seglen );
          vec3 brick = mix( vec3( 0.78, 0.76, 0.72 ), vTrim.rgb, step( 0.14, fract( v / 0.075 ) ) );
          diffuseColor.rgb = mix( base, brick, vTrim.a * corner );`,
      });
      m.normalMap.repeat.set(scale, scale); m.roughnessMap.repeat.set(scale, scale);
      return m;
    };
    const roof = (name, scale, mixOrtho, tint) => {
      const m = patch(new THREE.MeshStandardMaterial({ flatShading: true, side: THREE.DoubleSide, roughness: 1,
        normalMap: tex(`${name}_nor.jpg`, false), normalScale: new THREE.Vector2(1.2, 1.2), roughnessMap: tex(`${name}_rough.jpg`, false) }), {
        key: "toit" + name + mixOrtho, occ: 1.5, uniforms: bU({ tTex: { value: tex(`${name}_diff.jpg`) }, tOrtho: { value: this._ortho ||= tex("ortho.jpg", true, false) },
          uScale: { value: scale }, uSize: { value: S.size }, uMix: { value: mixOrtho }, uTintR: { value: new THREE.Vector4(...(tint || [0, 0, 0]), tint ? 0.6 : 0) } }),
        vpars: BLD_V, vmain: BLD_VM,
        fpars: BLD_F + `uniform sampler2D tTex, tOrtho; uniform float uScale, uSize, uMix; uniform vec4 uTintR;`,
        fcolor: `
          vec3 c = texture2D( tTex, vUv0 * uScale ).rgb * ( 0.8 + fract( vSeed ) * 0.4 );
          c = mix( c, uTintR.rgb, uTintR.a );
          vec3 oc = texture2D( tOrtho, vec2( vWP.x, - vWP.z ) / uSize ).rgb;
          diffuseColor.rgb = mix( c, oc * clamp( luma_( c ) / 0.42, 0.55, 1.5 ), uMix );`,
      });
      m.normalMap.repeat.set(scale, scale); m.roughnessMap.repeat.set(scale, scale);
      return m;
    };
    const peint = (key, { rough = 0.5, metal = 0, slats = 0, stripes = false, bars = false } = {}) => patch(
      new THREE.MeshStandardMaterial({ flatShading: true, side: THREE.DoubleSide, roughness: rough, metalness: metal }), {
        key, occ: 1.5, uniforms: bU({ uRough: { value: rough } }), vpars: BLD_V, vmain: BLD_VM, fpars: BLD_F + `uniform float uRough; float pn;`,
        fcolor: `
          pn = texture2D( uNoise, ( vWP.xz + vWP.y * vec2( 0.37, 0.61 ) ) / 2.0 ).r;
          vec3 c = vTint * ( 0.80 + pn * 0.34 );
          ${stripes ? "c = mix( c, vec3( 0.86, 0.84, 0.78 ), step( 0.55, vSeed ) * step( fract( vUv0.x / 0.26 ), 0.5 ) );" : ""}
          ${slats ? `c = mix( c, vec3( 0.02 ), step( fract( vUv0.y * ${slats.toFixed(1)} ), 0.16 ) * 0.65 );` : ""}
          ${bars ? "if ( max( step( fract( vUv0.x / 0.115 ), 0.17 ), max( step( 0.96, vUv0.y ), step( 0.06, vUv0.y ) * step( vUv0.y, 0.10 ) ) ) < 0.5 ) discard;" : ""}
          diffuseColor.rgb = c;`,
        frough: `roughnessFactor = uRough - 0.08 + pn * 0.16;`,
      });
    const vitre = patch(new THREE.MeshPhysicalMaterial({ flatShading: true, side: THREE.DoubleSide, roughness: 0.55, specularIntensity: 0, clearcoat: 1, clearcoatRoughness: 0.02 }), {
      key: "vitre", occ: 1.5, uniforms: bU({}), vpars: BLD_V, vmain: BLD_VM, fpars: BLD_F,
      fcolor: `
        float cur = step( 0.3, luma_( vTint ) );
        float fold = 0.78 + 0.22 * ( 0.5 + 0.5 * sin( vUv0.x * 56.5 + sin( vUv0.y * 3.0 ) * 1.5 ) );
        diffuseColor.rgb = vTint * mix( 1.0, fold, cur );`,
      femis: `totalEmissiveRadiance += vec3( 1.0, 0.80, 0.55 ) * ( step( 0.93, fract( vSeed * 37.7 ) ) * 1.2 + step( 0.5, vSeed ) * 0.05 );`,
    });
    const lambrequin = patch(new THREE.MeshStandardMaterial({ flatShading: true, side: THREE.DoubleSide, roughness: 0.6, color: new THREE.Color().setRGB(0.93, 0.91, 0.86) }), {
      key: "lambrequin", uniforms: bU({}), vpars: BLD_V, vmain: BLD_VM, fpars: BLD_F,
      fcolor: `
        float lx = fract( vUv0.x / 0.22 ) - 0.5;
        float edge = 0.04 + ( 1.0 - sqrt( max( 1.0 - lx * lx * 4.0, 0.0 ) ) ) * 0.12;
        vec2 hq = vec2( lx * 0.22, vUv0.y - 0.2 );
        if ( vUv0.y < edge || dot( hq, hq ) < 0.0016 ) discard;`,
    });
    const BM = {
      mur_enduit: wall("painted_plaster_wall", true, 1 / 2), mur_brique: wall("brick_4", false, 1 / 1.5), mur_pierre: wall("grey_plaster", true, 1 / 2.5),
      toit_tuile: roof("clay_roof_tiles_02", 1 / 2, 0.55), toit_ardoise: roof("roof_slates_02", 1 / 2, 0.55), toit_plat: roof("gravel_concrete", 1 / 3, 0.22, [0.30, 0.30, 0.29]),
      mur_leger: new THREE.MeshStandardMaterial({ flatShading: true, side: THREE.DoubleSide, roughness: 0.5, metalness: 0.3, color: new THREE.Color().setRGB(0.62, 0.62, 0.6) }),
      lambrequin, vitre, peint: peint("peint"), volet: peint("volet", { rough: 0.55, slats: 13 }),
      garde_corps: peint("garde_corps", { rough: 0.4, metal: 0.7, bars: true }), toile: peint("toile", { rough: 0.9, stripes: true }),
    };
    pending.push(GL.loadAsync(`${base}batiments.glb${q}`).then(g => {
      g.scene.traverse(o => {
        if (!o.isMesh) return;
        const ge = o.geometry;
        for (const [from, to] of [["color", "aCol"], ["uv1", "aUv1"], ["_trim", "aTrim"]]) if (ge.attributes[from]) { ge.setAttribute(to, ge.attributes[from]); ge.deleteAttribute(from); }
        o.material = BM[o.name] || BM.peint;
        o.material.shadowSide = THREE.BackSide;   // volumes fermés : la carte d'ombre garde les faces arrière, pas d'auto-ombrage des murs éclairés
        o.castShadow = !["garde_corps", "lambrequin", "vitre"].includes(o.name);
        o.receiveShadow = true;
      });
      root.add(g.scene);
    }));
    pending.push(GL.loadAsync(`${base}divers.glb${q}`).then(g => {
      g.scene.traverse(o => { if (o.isMesh) { o.receiveShadow = true; o.castShadow = !o.name.startsWith("lettres"); o.material.side = THREE.DoubleSide; o.material.shadowSide = THREE.BackSide; } });
      root.add(g.scene);
    }));

    // ---- voitures : un maillage par modèle × teinte, instancié ; version décimée au loin
    if (S.cars?.length) pending.push(GL.loadAsync(`${base}voitures.glb${q}`).then(g => {
      const carMat = patch(new THREE.MeshPhysicalMaterial({ clearcoat: 1, clearcoatRoughness: 0.06, side: THREE.DoubleSide, shadowSide: THREE.FrontSide }), {
        key: "voiture", occ: 1.3, uniforms: U({}), vpars: `attribute vec4 aCol; attribute vec4 aPbr; varying vec3 vTint; varying vec3 vPbr;`,
        vmain: `vTint = pow( aCol.rgb, vec3( 2.2 ) ); vPbr = aPbr.xyz;`, fpars: `varying vec3 vTint; varying vec3 vPbr;`,
        fcolor: `diffuseColor.rgb = vTint;`, frough: `roughnessFactor = vPbr.x;`, fmetal: `metalnessFactor = vPbr.y;`, fphys: `material.clearcoat *= vPbr.z;`,
      });
      const geo = {};
      g.scene.traverse(o => { if (o.isMesh) { const ge = o.geometry; ge.setAttribute("aCol", ge.attributes.color); ge.deleteAttribute("color"); ge.setAttribute("aPbr", ge.attributes._pbr); geo[o.name] = ge; } });
      const by = {};
      for (const c of S.cars) (by[c[0]] ||= []).push(c.slice(1));
      for (const [k, list] of Object.entries(by)) {
        if (!geo[`v${k}`]) continue;
        this.lods.push(new InstLOD(root, [{ geometry: geo[`v${k}`], material: carMat }], [{ geometry: geo[`v${k}_far`] || geo[`v${k}`], material: carMat }],
          new Float32Array(list.flat()), { nearDist: 110, maxDist: 1e9, cars: true }));
      }
    }));

    // ---- mobilier (modèles Poly Haven instanciés) et végétation (panneaux à vues multiples rendus par Cycles)
    const MAXD = { water_manhole_cover: 160, metal_trash_can: 400, planter_box_01: 400, standing_chalkboard_01: 450, outdoor_table_chair_set_01: 700, painted_wooden_bench: 700 };
    const m4 = new THREE.Matrix4(), qt = new THREE.Quaternion(), yUp = new THREE.Vector3(0, 1, 0);
    for (const [model, list] of Object.entries(S.instances || {})) {
      const imp = S.impostors?.[model];
      if (imp) { root.add(this.impostors(imp, list, TL)); continue; }
      if (!S.models?.[model]) continue;
      const mats = new Float32Array(list.length * 16);
      list.forEach(([x, y, z, rz, sc], i) => m4.compose(new THREE.Vector3(x, z, -y), qt.setFromAxisAngle(yUp, rz), new THREE.Vector3(sc, sc, sc)).toArray(mats, i * 16));
      pending.push(Promise.all([GL.loadAsync(`${base}models/${model}.glb${q}`), GL.loadAsync(`${base}models/${model}_far.glb${q}`).catch(() => null)]).then(([n, f]) => {
        const parts = (g, byName) => { const out = []; g.scene.updateMatrixWorld(true);
          g.scene.traverse(o => { if (o.isMesh) { const ge = o.geometry.clone().applyMatrix4(o.matrixWorld); out.push({ geometry: ge, material: byName ? (byName[o.material.name] || o.material) : o.material }); } });
          return out; };
        const near = parts(n), names = Object.fromEntries(near.map(p => [p.material.name, p.material]));
        this.lods.push(new InstLOD(root, near, f ? parts(f, names) : near, mats, { nearDist: 70, maxDist: MAXD[model] || 2500 }));
      }));
    }
    this.root = root;
    this.scene.add(root);
    await Promise.all(pending);
    await this.setShot(Object.keys(S.shots)[0], true);
    this.resize();
    this.renderer.compile(this.scene, this.camera);
    this.onStatus("");
    this.ready = true;
  }

  /** Champ de hauteur en tuiles, avec niveaux de détail selon la distance. hole : emprise couverte par un champ plus fin. */
  field(m, { material, smooth = false, steps = [1], dists = [0], hole = null, tile = 250 }) {
    const g = new THREE.Group();
    const { rows, cols, x0, y1, sx, sy } = m;
    this.pend.push(fetch(`${this.base}${m.file}${this.q}`).then(r => r.arrayBuffer()).then(buf => {
      const h = new Float32Array(buf);
      const seq = (a, b, st) => { const o = []; for (let i = a; i < b; i += st) o.push(i); o.push(b); return o; };
      for (let r0 = 0; r0 < rows - 1; r0 += tile) for (let c0 = 0; c0 < cols - 1; c0 += tile) {
        const r1 = Math.min(r0 + tile, rows - 1), c1 = Math.min(c0 + tile, cols - 1);
        const xa = x0 + c0 * sx, xb = x0 + c1 * sx, yb = y1 - r0 * sy, ya = y1 - r1 * sy;
        if (hole && xa >= hole[0] && xb <= hole[2] && ya >= hole[1] && yb <= hole[3]) continue;
        const cx = (xa + xb) / 2, cy = (ya + yb) / 2;
        const lod = new THREE.LOD();
        lod.position.set(cx, 0, -cy);
        steps.forEach((st, li) => {
          const rs = seq(r0, r1, st), cs = seq(c0, c1, st), nr = rs.length, nc = cs.length;
          const pos = new Float32Array(nr * nc * 3), nrm = smooth ? new Float32Array(nr * nc * 3) : null;
          let k = 0;
          for (const r of rs) for (const c of cs) {
            pos[k] = x0 + c * sx - cx; pos[k + 1] = h[r * cols + c]; pos[k + 2] = -(y1 - r * sy) + cy;
            if (nrm) {
              const dx = (h[r * cols + Math.min(c + 1, cols - 1)] - h[r * cols + Math.max(c - 1, 0)]) / (2 * sx);
              const dn = (h[Math.max(r - 1, 0) * cols + c] - h[Math.min(r + 1, rows - 1) * cols + c]) / (2 * sy);
              const l = Math.hypot(dx, dn, 1);
              nrm[k] = -dx / l; nrm[k + 1] = 1 / l; nrm[k + 2] = dn / l;
            }
            k += 3;
          }
          const idx = new (nr * nc > 65535 ? Uint32Array : Uint16Array)((nr - 1) * (nc - 1) * 6);
          k = 0;
          for (let i = 0; i < nr - 1; i++) for (let j = 0; j < nc - 1; j++) {
            const a = i * nc + j, b = a + nc, d = a + 1, c = b + 1;
            idx[k++] = a; idx[k++] = b; idx[k++] = d; idx[k++] = b; idx[k++] = c; idx[k++] = d;
          }
          const ge = new THREE.BufferGeometry();
          ge.setAttribute("position", new THREE.BufferAttribute(pos, 3));
          if (nrm) ge.setAttribute("normal", new THREE.BufferAttribute(nrm, 3));
          ge.setIndex(new THREE.BufferAttribute(idx, 1));
          const mesh = new THREE.Mesh(ge, material);
          mesh.receiveShadow = true;
          lod.addLevel(mesh, dists[li]);
        });
        g.add(lod);
      }
    }));
    return g;
  }

  /** Arbres et arbustes : un panneau par instance, orienté vers la caméra, qui choisit la vue de l'atlas la plus proche. */
  impostors(imp, list, TL) {
    const t = TL.load(`${this.base}${imp.atlas}${this.q}`);
    t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 8;
    const n = list.length, iPos = new Float32Array(n * 3), iPar = new Float32Array(n * 3);
    const [cx, cy, cz] = imp.center;
    list.forEach(([x, y, z, rz, sc], i) => {
      const co = Math.cos(rz), si = Math.sin(rz);
      const ax = x + (cx * co - cy * si) * sc, ay = y + (cx * si + cy * co) * sc, az = z + cz * sc;
      iPos.set([ax, az, -ay], i * 3); iPar.set([2 * imp.radius * sc, rz, (i * 0.618) % 1], i * 3);
    });
    const ge = new THREE.InstancedBufferGeometry().copy(new THREE.PlaneGeometry(1, 1));
    ge.instanceCount = n;
    ge.setAttribute("iPos", new THREE.InstancedBufferAttribute(iPos, 3));
    ge.setAttribute("iPar", new THREE.InstancedBufferAttribute(iPar, 3));
    const el0 = imp.els[0], elStep = imp.els.length > 1 ? imp.els[1] - imp.els[0] : 1;
    const IMP = (shadow) => `
      vec3 toCam = ${shadow ? "uSunDir" : "normalize( cameraPosition - iPos )"};
      float ei = clamp( floor( ( degrees( asin( clamp( toCam.y, -1.0, 1.0 ) ) ) - ${el0.toFixed(2)} ) / ${elStep.toFixed(2)} + 0.5 ), 0.0, ${(imp.rows - 1).toFixed(1)} );
      float ai = mod( floor( ( atan( - toCam.z, toCam.x ) - iPar.y ) / 6.2831853 * ${imp.cols.toFixed(1)} + 0.5 ), ${imp.cols.toFixed(1)} );
      float elr = radians( ${el0.toFixed(2)} + ei * ${elStep.toFixed(2)} ), azr = ai / ${imp.cols.toFixed(1)} * 6.2831853 + iPar.y;
      vec3 iD = vec3( cos( elr ) * cos( azr ), sin( elr ), - cos( elr ) * sin( azr ) );
      vec3 iR = normalize( cross( vec3( 0.0, 1.0, 0.0 ), iD ) ); vec3 iU = cross( iD, iR );
      vec3 impPos = iPos + ( iR * position.x + iU * position.y ) * iPar.x;
      #ifdef USE_MAP
        vMapUv = ( vec2( ai, ei ) + uv ) / vec2( ${imp.cols.toFixed(1)}, ${imp.rows.toFixed(1)} );
      #endif
`;
    const vpars = `attribute vec3 iPos; attribute vec3 iPar; uniform vec3 uSunDir;`;
    const mat = patch(new THREE.MeshStandardMaterial({ map: t, alphaTest: 0.42, roughness: 1, side: THREE.DoubleSide, alphaToCoverage: true }), {
      key: "imp" + imp.atlas, occ: 1.6, uniforms: { uNoise: this.uNoise, uSunDir: this.uSun }, vpars,
      vnormal: IMP(false) + `
        vec2 sp = position.xy * 2.0;
        vec3 objectNormal = normalize( ( iR * sp.x + iU * sp.y ) * 0.3 + iD * 0.55 + vec3( 0.0, 0.8, 0.0 ) );`,
      vbegin: `vec3 transformed = impPos;`,
      vshadow: `#ifdef USE_SHADOWMAP\n worldPosition.xyz += uSunDir * iPar.x * 0.56;\n#endif`,
      fcolor: `diffuseColor.rgb *= vec3( 0.45, 0.52, 0.38 );`,
    });
    const dep = patch(new THREE.MeshDepthMaterial({ map: t, alphaTest: 0.42, side: THREE.DoubleSide }), {
      key: "impd" + imp.atlas, uniforms: { uNoise: this.uNoise, uSunDir: this.uSun }, vpars, vbegin: IMP(true) + `vec3 transformed = impPos;`,
    });
    const mesh = new THREE.Mesh(ge, mat);
    mesh.customDepthMaterial = dep;
    mesh.frustumCulled = false; mesh.castShadow = mesh.receiveShadow = true;
    return mesh;
  }

  // ------------------------------------------------------------------ ciel, soleil, points de vue
  async setShot(name, moveCamera = true) {
    const s = this.S.shots[name];
    if (!s) return;
    this.shot = name;
    const h = this.S.hdris[s.hdri];
    if (!this.hdr[s.hdri]) {
      const t = await new HDRLoader().setDataType(THREE.FloatType).loadAsync(`${this.base}${h.file}${this.q}`);
      const d = t.image.data;
      for (let i = 0; i < d.length; i++) if (d[i] > 6) d[i] = 6;      // on bride le disque solaire : le soleil vient de la lampe
      t.mapping = THREE.EquirectangularReflectionMapping; t.needsUpdate = true;
      const pm = new THREE.PMREMGenerator(this.renderer);
      this.hdr[s.hdri] = { bg: t, env: pm.fromEquirectangular(t).texture };
      pm.dispose();
    }
    const sc = this.scene, D2R = Math.PI / 180;
    sc.background = this.hdr[s.hdri].bg; sc.environment = this.hdr[s.hdri].env;
    const rot = (s.sun_az - (180 + h.sun_az)) * D2R;
    sc.backgroundRotation.set(0, rot, 0); sc.environmentRotation.set(0, rot, 0);
    sc.backgroundIntensity = sc.environmentIntensity = s.sky;
    const az = s.sun_az * D2R, el = s.sun_el * D2R;
    this.uSun.value.set(Math.cos(el) * Math.cos(az), Math.sin(el), -Math.cos(el) * Math.sin(az));
    this.sun.color.setRGB(...s.sun_color); this.sun.intensity = s.sun;
    const L = 0.34 * (s.sun * Math.sin(el) + 2.5 * s.sky) / Math.PI;
    this.bounce.groundColor.setRGB(s.sun_color[0], s.sun_color[1] * 0.96, s.sun_color[2] * 0.9); this.bounce.intensity = Math.PI * L;
    this.final.material.uniforms.toneMappingExposure.value = Math.pow(2, s.exposure);
    const k = s.haze.amount * 1.8 * (9000 / Math.max(s.haze.depth, 1));
    this.fog.color.setRGB(h.horizon[0] * s.sky, h.horizon[1] * s.sky, h.horizon[2] * s.sky);
    this.fog.near = s.haze.start; this.fog.far = s.haze.start + 22000 / Math.max(k, 1e-3);
    if (moveCamera) {
      const [x, y, z] = s.cam, [tx, ty, tz] = s.target;
      const p = new THREE.Vector3(x, z, -y), t = new THREE.Vector3(tx, tz, -ty);
      this.hfov = THREE.MathUtils.radToDeg(2 * Math.atan(18 / s.lens));
      this.camera.position.copy(p);
      this.skipCars = s.street ? p.clone() : null; this._lodPos = null;
      // à hauteur de rue, on pivote autour d'un point proche (sinon la caméra tourne autour d'une cible à 200 m)
      const dir = t.clone().sub(p), dist = dir.length();
      this.controls.target.copy(p).addScaledVector(dir.normalize(), s.street ? Math.min(dist, 12) : dist);
      this.controls.update();
      this.resize();
    }
  }

  // ------------------------------------------------------------------ boucle de rendu
  render() {
    const dt = Math.min(this.clock.getDelta(), 0.1), cam = this.camera, ctl = this.controls;
    this.uTime.value += dt;
    // déplacement au clavier (flèches / ZQSD / WASD), vitesse proportionnelle à la hauteur
    if (this.keys.size) {
      const f = new THREE.Vector3(); cam.getWorldDirection(f); f.y = 0; f.normalize();
      const r = new THREE.Vector3(-f.z, 0, f.x), mv = new THREE.Vector3();
      const K = (...c) => c.some(x => this.keys.has(x));
      if (K("ArrowUp", "KeyW", "KeyZ")) mv.add(f); if (K("ArrowDown", "KeyS")) mv.sub(f);
      if (K("ArrowRight", "KeyD")) mv.add(r); if (K("ArrowLeft", "KeyA", "KeyQ")) mv.sub(r);
      if (mv.lengthSq()) { mv.normalize().multiplyScalar(dt * Math.max(4, cam.position.y * 0.8) * (K("ShiftLeft", "ShiftRight") ? 3 : 1)); cam.position.add(mv); ctl.target.add(mv); }
    }
    ctl.update();
    const dist = cam.position.distanceTo(ctl.target);
    cam.near = THREE.MathUtils.clamp(Math.min(dist, cam.position.y) * 0.04, 0.3, 12);
    cam.far = 90000; cam.updateProjectionMatrix();
    // carte d'ombre recentrée sur ce que regarde la caméra, d'autant plus fine qu'on est près
    const E = THREE.MathUtils.clamp(dist * 1.1, 45, 800), sh = this.sun.shadow, texel = 2 * E / sh.mapSize.x;
    const focus = ctl.target.clone();
    this.sun.target.position.copy(focus);
    this.sun.position.copy(focus).addScaledVector(this.uSun.value, 1500);
    Object.assign(sh.camera, { left: -E, right: E, top: E, bottom: -E, near: 10, far: 3000 });
    sh.camera.updateProjectionMatrix();
    sh.bias = -texel * 1.5 / 2990; sh.normalBias = texel * 1.0; sh.radius = 1.2;
    if (!this._lodPos || this._lodPos.distanceToSquared(cam.position) > 25) {
      (this._lodPos ||= new THREE.Vector3()).copy(cam.position);
      for (const l of this.lods) l.update(cam.position, this.skipCars);
    }
    const r = this.renderer;
    r.setRenderTarget(this.rt); r.render(this.scene, cam);
    this.tris = r.info.render.triangles; this.calls = r.info.render.calls;
    let src = this.rt;
    if (this.ao) { this.gtao.render(r, this.rtAO, this.rt); src = this.rtAO; }
    this.final.material.uniforms.tDiffuse.value = src.texture;
    r.setRenderTarget(null); this.final.render(r);
    if (++this.frames >= 30) {
      const now = performance.now(); this.fps = 30000 / (now - this._t); this._t = now; this.frames = 0;
      // résolution adaptative : on garde la fluidité sur les écrans à forte densité
      const pr = r.getPixelRatio();
      if (this.fps < 24 && pr > 1) { r.setPixelRatio(Math.max(1, pr - 0.25)); this.resize(); }
    }
  }

  dispose() {
    if (!this.root) return;
    this.scene.remove(this.root);
    this.root.traverse(o => { o.geometry?.dispose?.(); });
    for (const l of this.lods) l.dispose();
    this.lods = []; this.root = null; this.ready = false; this._ortho = null; this._lodPos = null;
  }
}

/** Instances avec deux niveaux de détail : complet près de la caméra, décimé au loin, masqué au-delà de maxDist. */
class InstLOD {
  constructor(parent, near, far, mats, { nearDist, maxDist, cars = false }) {
    this.cars = cars;
    this.mats = mats; this.n = mats.length / 16; this.nearDist2 = nearDist * nearDist; this.maxDist2 = maxDist * maxDist;
    this.aN = new THREE.InstancedBufferAttribute(new Float32Array(mats.length), 16); this.aF = new THREE.InstancedBufferAttribute(new Float32Array(mats.length), 16);
    this.aN.setUsage(THREE.DynamicDrawUsage); this.aF.setUsage(THREE.DynamicDrawUsage);
    const mk = (parts, attr) => parts.map(p => {
      const m = new THREE.InstancedMesh(p.geometry, p.material, this.n);
      m.instanceMatrix = attr; m.count = 0; m.frustumCulled = false; m.castShadow = m.receiveShadow = true;
      parent.add(m); return m;
    });
    this.near = mk(near, this.aN); this.far = mk(far, this.aF);
  }
  update(p, skip) {
    const m = this.mats, an = this.aN.array, af = this.aF.array;
    if (!this.cars) skip = null;
    let kn = 0, kf = 0;
    for (let i = 0; i < this.n; i++) {
      const o = i * 16, dx = m[o + 12] - p.x, dy = m[o + 13] - p.y, dz = m[o + 14] - p.z, d2 = dx * dx + dy * dy + dz * dz;
      if (d2 > this.maxDist2) continue;
      if (skip && (m[o + 12] - skip.x) ** 2 + (m[o + 14] - skip.z) ** 2 < 400) continue;
      if (d2 < this.nearDist2) { for (let j = 0; j < 16; j++) an[kn * 16 + j] = m[o + j]; kn++; }
      else { for (let j = 0; j < 16; j++) af[kf * 16 + j] = m[o + j]; kf++; }
    }
    this.aN.needsUpdate = this.aF.needsUpdate = true;
    for (const x of this.near) x.count = kn;
    for (const x of this.far) x.count = kf;
  }
  dispose() { for (const x of [...this.near, ...this.far]) { x.parent?.remove(x); x.dispose(); } }
}
