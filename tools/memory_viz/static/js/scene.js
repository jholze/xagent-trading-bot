/**
 * Three.js cortex scene — points, glow hits, orbit, idle spin.
 * ES module only (no Node require).
 */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

export class CortexScene {
  /**
   * @param {HTMLCanvasElement} canvas
   */
  constructor(canvas) {
    this.canvas = canvas;
    this.nodes = [];
    this.hitSet = new Set();
    this.enabledLobes = new Set();
    this.enabledSymbols = null; // null = all
    this._selected = -1;
    this._idleSince = performance.now();
    this._onPick = null;

    this.renderer = new THREE.WebGLRenderer({
      canvas,
      antialias: true,
      alpha: true,
      powerPreference: "high-performance",
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.setSize(window.innerWidth, window.innerHeight, false);
    this.renderer.setClearColor(0x000000, 0);

    this.scene = new THREE.Scene();
    this.scene.fog = new THREE.FogExp2(0x05060a, 0.045);

    this.camera = new THREE.PerspectiveCamera(55, window.innerWidth / window.innerHeight, 0.05, 80);
    this.camera.position.set(0.4, 0.6, 4.2);

    this.controls = new OrbitControls(this.camera, this.canvas);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.06;
    this.controls.minDistance = 1.2;
    this.controls.maxDistance = 14;
    this.controls.addEventListener("start", () => {
      this._idleSince = performance.now() + 1e9;
    });
    this.controls.addEventListener("end", () => {
      this._idleSince = performance.now();
    });

    // ambient dust
    this._addDust();

    this.points = null;
    this.geom = null;
    this.mat = null;
    this._baseColors = null;
    this._baseSizes = null;

    this.raycaster = new THREE.Raycaster();
    this.raycaster.params.Points = { threshold: 0.08 };
    this._pointer = new THREE.Vector2();

    this.canvas.addEventListener("pointerdown", (e) => this._onPointer(e));
    window.addEventListener("resize", () => this._resize());

    this._raf = 0;
    this._tick = this._tick.bind(this);
  }

  onPick(fn) {
    this._onPick = fn;
  }

  /**
   * @param {Array<{pos:number[], col:number[], lobe:string, symbol?:string}>} nodes
   */
  setNodes(nodes) {
    this.nodes = nodes || [];
    if (this.points) {
      this.scene.remove(this.points);
      this.geom?.dispose();
      this.mat?.dispose();
    }
    const n = this.nodes.length;
    const positions = new Float32Array(n * 3);
    const colors = new Float32Array(n * 3);
    const sizes = new Float32Array(n);
    this._baseColors = new Float32Array(n * 3);
    this._baseSizes = new Float32Array(n);

    for (let i = 0; i < n; i++) {
      const p = this.nodes[i].pos || [0, 0, 0];
      const c = this.nodes[i].col || [0.5, 0.5, 0.5];
      positions[i * 3] = p[0];
      positions[i * 3 + 1] = p[1];
      positions[i * 3 + 2] = p[2];
      colors[i * 3] = c[0];
      colors[i * 3 + 1] = c[1];
      colors[i * 3 + 2] = c[2];
      this._baseColors[i * 3] = c[0];
      this._baseColors[i * 3 + 1] = c[1];
      this._baseColors[i * 3 + 2] = c[2];
      const sz = 0.055 + (i % 5) * 0.006;
      sizes[i] = sz;
      this._baseSizes[i] = sz;
    }

    this.geom = new THREE.BufferGeometry();
    this.geom.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    this.geom.setAttribute("color", new THREE.BufferAttribute(colors, 3));
    this.geom.setAttribute("size", new THREE.BufferAttribute(sizes, 1));

    this.mat = new THREE.ShaderMaterial({
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      vertexColors: true,
      uniforms: {
        uTime: { value: 0 },
        uPixelRatio: { value: this.renderer.getPixelRatio() },
      },
      vertexShader: `
        attribute float size;
        varying vec3 vColor;
        varying float vAlpha;
        uniform float uPixelRatio;
        void main() {
          vColor = color;
          vAlpha = 1.0;
          vec4 mv = modelViewMatrix * vec4(position, 1.0);
          gl_PointSize = size * uPixelRatio * (280.0 / -mv.z);
          gl_Position = projectionMatrix * mv;
        }
      `,
      fragmentShader: `
        varying vec3 vColor;
        varying float vAlpha;
        void main() {
          vec2 uv = gl_PointCoord - 0.5;
          float d = length(uv);
          float a = smoothstep(0.5, 0.08, d) * vAlpha;
          if (a < 0.02) discard;
          gl_FragColor = vec4(vColor, a);
        }
      `,
    });

    // ShaderMaterial + vertexColors needs color attribute as `color`
    this.geom.setAttribute("color", this.geom.getAttribute("color"));

    this.points = new THREE.Points(this.geom, this.mat);
    this.scene.add(this.points);
    this.applyVisibility();
  }

  /**
   * Append nodes and flash them as new memory arrivals.
   * @param {Array} nodes
   */
  appendNodes(nodes) {
    if (!nodes || !nodes.length) return;
    const start = this.nodes.length;
    const merged = this.nodes.concat(nodes);
    this.setNodes(merged);
    const indices = [];
    for (let i = 0; i < nodes.length; i++) indices.push(start + i);
    this.flashNew(indices);
  }

  flashNew(indices) {
    this.hitSet = new Set(indices || []);
    this.applyVisibility();
    if (indices && indices.length) {
      this.selectIndex(indices[indices.length - 1]);
    }
    // clear flash after a few seconds so cortex settles
    clearTimeout(this._flashT);
    this._flashT = setTimeout(() => {
      // keep last selected; clear hit glow
      this.clearHits();
    }, 4500);
  }

  setHits(indices) {
    this.hitSet = new Set(indices || []);
    this.applyVisibility();
  }

  clearHits() {
    this.hitSet = new Set();
    this.applyVisibility();
  }

  setLobeEnabled(lobe, on) {
    if (on) this.enabledLobes.add(lobe);
    else this.enabledLobes.delete(lobe);
    this.applyVisibility();
  }

  setAllLobes(lobes) {
    this.enabledLobes = new Set(lobes || []);
    this.applyVisibility();
  }

  setSymbolFilter(symbols) {
    // null = all; Set of symbols
    this.enabledSymbols = symbols;
    this.applyVisibility();
  }

  selectIndex(i) {
    this._selected = i;
    this.applyVisibility();
    if (i >= 0 && this.nodes[i]) {
      const p = this.nodes[i].pos;
      this._flyTo(p[0], p[1], p[2]);
    }
  }

  resetCamera() {
    this.camera.position.set(0.4, 0.6, 4.2);
    this.controls.target.set(0, 0, 0);
    this.controls.update();
    this._idleSince = performance.now();
  }

  applyVisibility() {
    if (!this.geom || !this._baseColors) return;
    const colors = this.geom.getAttribute("color");
    const sizes = this.geom.getAttribute("size");
    const hasHits = this.hitSet.size > 0;
    const t = performance.now() * 0.004;

    for (let i = 0; i < this.nodes.length; i++) {
      const node = this.nodes[i];
      const lobeOk = this.enabledLobes.size === 0 || this.enabledLobes.has(node.lobe);
      let symOk = true;
      if (this.enabledSymbols && this.enabledSymbols.size > 0) {
        const s = (node.symbol || "").split("/")[0] || "";
        symOk = this.enabledSymbols.has(s) || this.enabledSymbols.has(node.symbol);
      }
      const visible = lobeOk && symOk;
      const isHit = this.hitSet.has(i);
      const isSel = i === this._selected;

      let mul = 1.0;
      if (!visible) mul = 0.04;
      else if (hasHits && !isHit) mul = 0.12;
      else if (isHit) mul = 1.35 + 0.15 * Math.sin(t + i);
      if (isSel) mul = Math.max(mul, 1.5);

      colors.setXYZ(
        i,
        this._baseColors[i * 3] * mul,
        this._baseColors[i * 3 + 1] * mul,
        this._baseColors[i * 3 + 2] * mul
      );
      let sz = this._baseSizes[i];
      if (isHit) sz *= 1.8;
      if (isSel) sz *= 2.2;
      if (!visible) sz *= 0.3;
      sizes.setX(i, sz);
    }
    colors.needsUpdate = true;
    sizes.needsUpdate = true;
  }

  start() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(this._tick);
  }

  _tick(now) {
    this._raf = requestAnimationFrame(this._tick);
    if (this.mat) this.mat.uniforms.uTime.value = now * 0.001;
    // idle orbit
    if (now - this._idleSince > 8000) {
      this.controls.autoRotate = true;
      this.controls.autoRotateSpeed = 0.4;
    } else {
      this.controls.autoRotate = false;
    }
    if (this.hitSet.size > 0) this.applyVisibility();
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  }

  _resize() {
    const w = window.innerWidth;
    const h = window.innerHeight;
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h, false);
    if (this.mat) this.mat.uniforms.uPixelRatio.value = this.renderer.getPixelRatio();
  }

  _addDust() {
    const n = 400;
    const pos = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) {
      pos[i * 3] = (Math.random() - 0.5) * 12;
      pos[i * 3 + 1] = (Math.random() - 0.5) * 12;
      pos[i * 3 + 2] = (Math.random() - 0.5) * 12;
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    const m = new THREE.PointsMaterial({
      color: 0x334155,
      size: 0.015,
      transparent: true,
      opacity: 0.35,
      depthWrite: false,
    });
    this.scene.add(new THREE.Points(g, m));
  }

  _onPointer(e) {
    if (!this.points) return;
    const rect = this.canvas.getBoundingClientRect();
    this._pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    this._pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
    this.raycaster.setFromCamera(this._pointer, this.camera);
    const hits = this.raycaster.intersectObject(this.points, false);
    if (hits.length && hits[0].index != null) {
      const idx = hits[0].index;
      this.selectIndex(idx);
      if (this._onPick) this._onPick(idx, this.nodes[idx]);
    }
  }

  _flyTo(x, y, z) {
    this.controls.target.set(x, y, z);
    const dir = new THREE.Vector3()
      .subVectors(this.camera.position, this.controls.target)
      .normalize()
      .multiplyScalar(2.4);
    this.camera.position.set(x + dir.x, y + dir.y + 0.3, z + dir.z);
    this.controls.update();
    this._idleSince = performance.now();
  }
}
