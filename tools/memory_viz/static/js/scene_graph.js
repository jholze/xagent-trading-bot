/**
 * Mode 2 — Knowledge Graph 3D (force-directed synapses).
 * Cartographer-style glow + GraphAura-style force links.
 * ES module only.
 */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const KIND_COLOR = {
  semantic: 0x22d3ee,
  symbol: 0xf472b6,
  lobe: 0xa78bfa,
  "semantic+symbol": 0xfbbf24,
  "semantic+lobe": 0x67e8f9,
  "symbol+lobe": 0xe879f9,
};

export class GraphScene {
  /**
   * @param {HTMLCanvasElement} canvas
   */
  constructor(canvas) {
    this.canvas = canvas;
    this.nodes = [];
    this.links = [];
    this._sim = [];
    this._linkIdx = [];
    this.hitSet = new Set();
    this.enabledLobes = new Set();
    this.enabledSymbols = null;
    this._selected = -1;
    this._hovered = -1;
    this._onPick = null;
    this._onHover = null;
    this._flashUntil = 0;
    this._idleSince = performance.now();
    this._alpha = 1; // simulation cooling
    this._visible = false;

    this.renderer = new THREE.WebGLRenderer({
      canvas,
      antialias: true,
      alpha: true,
      powerPreference: "high-performance",
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.setSize(window.innerWidth, window.innerHeight, false);
    this.renderer.setClearColor(0x000000, 0);
    this.renderer.domElement.style.display = "none";

    this.scene = new THREE.Scene();
    this.scene.fog = new THREE.FogExp2(0x03040a, 0.028);

    this.camera = new THREE.PerspectiveCamera(55, window.innerWidth / window.innerHeight, 0.1, 200);
    this.camera.position.set(0, 8, 28);

    this.controls = new OrbitControls(this.camera, this.canvas);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.07;
    this.controls.minDistance = 4;
    this.controls.maxDistance = 90;
    this.controls.zoomSpeed = 2.6;
    this.controls.rotateSpeed = 0.85;
    this._distMin = 4;
    this._distMax = 90;
    this.controls.addEventListener("start", () => {
      this._idleSince = performance.now() + 1e9;
    });
    this.controls.addEventListener("end", () => {
      this._idleSince = performance.now();
    });
    this.controls.addEventListener("change", () => {
      if (this._onZoomChange) this._onZoomChange(this.getZoomSliderValue());
    });

    // soft lights
    this.scene.add(new THREE.AmbientLight(0x6688aa, 0.55));
    const key = new THREE.PointLight(0x22d3ee, 1.2, 80);
    key.position.set(12, 18, 10);
    this.scene.add(key);
    const fill = new THREE.PointLight(0xa78bfa, 0.6, 60);
    fill.position.set(-14, 6, -8);
    this.scene.add(fill);

    this._addBackdrop();

    this.nodeGroup = new THREE.Group();
    this.linkGroup = new THREE.Group();
    this.particleGroup = new THREE.Group();
    this.labelGroup = new THREE.Group();
    this.scene.add(this.nodeGroup);
    this.scene.add(this.linkGroup);
    this.scene.add(this.particleGroup);
    this.scene.add(this.labelGroup);

    this._nodeMeshes = [];
    this._linkLines = null;
    this._particles = null;
    this._particleData = [];

    this.raycaster = new THREE.Raycaster();
    this.raycaster.params.Points = { threshold: 0.4 };
    this._pointer = new THREE.Vector2();

    this.canvas.addEventListener("pointerdown", (e) => this._onPointer(e));
    this.canvas.addEventListener("pointermove", (e) => this._onMove(e));
    window.addEventListener("resize", () => this._resize());

    this._raf = 0;
    this._tick = this._tick.bind(this);
  }

  setVisible(on) {
    this._visible = !!on;
    this.renderer.domElement.style.display = on ? "block" : "none";
    if (on) {
      this._resize();
      this._idleSince = performance.now();
    }
  }

  onPick(fn) {
    this._onPick = fn;
  }
  onHover(fn) {
    this._onHover = fn;
  }
  onZoomChange(fn) {
    this._onZoomChange = fn;
  }

  getDistance() {
    return this.camera.position.distanceTo(this.controls.target);
  }

  getZoomSliderValue() {
    const d = this.getDistance();
    const t =
      (Math.log(d) - Math.log(this._distMin)) /
      (Math.log(this._distMax) - Math.log(this._distMin));
    return Math.max(0, Math.min(100, (1 - t) * 100));
  }

  setZoomFromSlider(value) {
    const v = Math.max(0, Math.min(100, Number(value) || 0));
    const t = 1 - v / 100;
    const dist = Math.exp(
      Math.log(this._distMin) + t * (Math.log(this._distMax) - Math.log(this._distMin))
    );
    this._setDistance(dist);
  }

  zoomIn(step = 0.75) {
    this._setDistance(this.getDistance() * step);
  }
  zoomOut(step = 1.35) {
    this._setDistance(this.getDistance() * step);
  }

  _setDistance(dist) {
    const d = Math.max(this._distMin, Math.min(this._distMax, dist));
    const dir = new THREE.Vector3()
      .subVectors(this.camera.position, this.controls.target)
      .normalize();
    if (dir.lengthSq() < 1e-8) dir.set(0.2, 0.4, 1).normalize();
    this.camera.position.copy(this.controls.target).addScaledVector(dir, d);
    this.controls.update();
    if (this._onZoomChange) this._onZoomChange(this.getZoomSliderValue());
  }

  resetCamera() {
    this.camera.position.set(0, 8, 28);
    this.controls.target.set(0, 0, 0);
    this.controls.update();
    this._idleSince = performance.now();
    if (this._onZoomChange) this._onZoomChange(this.getZoomSliderValue());
  }

  /**
   * @param {{nodes: any[], links: any[]}} graph
   */
  setGraph(graph) {
    this.nodes = (graph && graph.nodes) || [];
    this.links = (graph && graph.links) || [];
    this._rebuild();
    this._alpha = 1;
  }

  appendNodes(nodes, links) {
    if (!nodes || !nodes.length) return;
    const start = this.nodes.length;
    // map links that use store indices → may need id mapping
    const byId = new Map(this.nodes.map((n, i) => [n.id, i]));
    for (const n of nodes) {
      if (n && n.id && !byId.has(n.id)) {
        byId.set(n.id, this.nodes.length);
        this.nodes.push(n);
      }
    }
    if (links && links.length) {
      for (const L of links) {
        let s2 = -1;
        let t2 = -1;
        if (L.source_id) s2 = byId.has(L.source_id) ? byId.get(L.source_id) : -1;
        if (L.target_id) t2 = byId.has(L.target_id) ? byId.get(L.target_id) : -1;
        if (s2 < 0 && typeof L.source === "number") {
          s2 = this.nodes.findIndex((n) => n.i === L.source || n.gi === L.source);
        }
        if (t2 < 0 && typeof L.target === "number") {
          t2 = this.nodes.findIndex((n) => n.i === L.target || n.gi === L.target);
        }
        if (s2 < 0 || t2 < 0 || s2 === t2) continue;
        this.links.push({
          source: Math.min(s2, t2),
          target: Math.max(s2, t2),
          weight: L.weight || 0.4,
          kind: L.kind || "semantic",
        });
      }
    }
    this._rebuild();
    this._alpha = Math.max(this._alpha, 0.55);
    const indices = [];
    for (let i = start; i < this.nodes.length; i++) indices.push(i);
    this.flashNew(indices);
  }

  flashNew(indices) {
    this.hitSet = new Set(indices || []);
    this._selected = -1;
    this._flashUntil = performance.now() + 4200;
    this._applyVisuals();
  }

  setHits(indices) {
    this.hitSet = new Set(indices || []);
    this._applyVisuals();
  }

  clearHits() {
    this.hitSet = new Set();
    this._applyVisuals();
  }

  setLobeEnabled(lobe, on) {
    if (on) this.enabledLobes.add(lobe);
    else this.enabledLobes.delete(lobe);
    this._applyVisuals();
  }

  setAllLobes(lobes) {
    this.enabledLobes = new Set(lobes || []);
    this._applyVisuals();
  }

  setSymbolFilter(symbols) {
    this.enabledSymbols = symbols;
    this._applyVisuals();
  }

  selectIndex(i) {
    this._selected = i;
    this._applyVisuals();
    if (i >= 0 && this._sim[i]) {
      this._flyTo(this._sim[i].x, this._sim[i].y, this._sim[i].z);
    }
  }

  highlightByIds(ids) {
    const set = new Set(ids || []);
    const indices = [];
    this.nodes.forEach((n, i) => {
      if (set.has(n.id)) indices.push(i);
    });
    this.setHits(indices);
    if (indices.length) this.selectIndex(indices[0]);
  }

  start() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(this._tick);
  }

  _rebuild() {
    // clear groups
    while (this.nodeGroup.children.length) {
      const c = this.nodeGroup.children.pop();
      c.geometry?.dispose?.();
      c.material?.dispose?.();
    }
    while (this.linkGroup.children.length) {
      const c = this.linkGroup.children.pop();
      c.geometry?.dispose?.();
      c.material?.dispose?.();
    }
    while (this.particleGroup.children.length) {
      const c = this.particleGroup.children.pop();
      c.geometry?.dispose?.();
      c.material?.dispose?.();
    }
    while (this.labelGroup.children.length) {
      this.labelGroup.remove(this.labelGroup.children[0]);
    }
    this._nodeMeshes = [];
    this._particleData = [];

    const n = this.nodes.length;
    // init sim from lobe-ish positions
    this._sim = [];
    for (let i = 0; i < n; i++) {
      const p = this.nodes[i].pos || [0, 0, 0];
      const seed = 0.15;
      this._sim.push({
        x: (p[0] || 0) * 4 + (Math.random() - 0.5) * seed,
        y: (p[1] || 0) * 4 + (Math.random() - 0.5) * seed,
        z: (p[2] || 0) * 4 + (Math.random() - 0.5) * seed,
        vx: 0,
        vy: 0,
        vz: 0,
      });
    }

    // node meshes
    const geo = new THREE.SphereGeometry(1, 16, 16);
    for (let i = 0; i < n; i++) {
      const node = this.nodes[i];
      const col = node.col || [0.4, 0.5, 0.6];
      const mat = new THREE.MeshStandardMaterial({
        color: new THREE.Color(col[0], col[1], col[2]),
        emissive: new THREE.Color(col[0], col[1], col[2]),
        emissiveIntensity: 0.55,
        metalness: 0.2,
        roughness: 0.35,
        transparent: true,
        opacity: 0.95,
      });
      const mesh = new THREE.Mesh(geo, mat);
      const deg = node.degree || node.val || 1;
      const r = 0.18 + Math.min(0.55, deg * 0.035);
      mesh.scale.setScalar(r);
      mesh.userData.index = i;
      this.nodeGroup.add(mesh);
      this._nodeMeshes.push(mesh);
    }

    // links as single LineSegments
    this._linkIdx = this.links.map((L) => ({
      s: L.source,
      t: L.target,
      w: Math.max(0.05, Math.min(1, L.weight || 0.3)),
      kind: L.kind || "semantic",
    }));
    const positions = new Float32Array(this._linkIdx.length * 6);
    const colors = new Float32Array(this._linkIdx.length * 6);
    for (let i = 0; i < this._linkIdx.length; i++) {
      const L = this._linkIdx[i];
      const c = new THREE.Color(KIND_COLOR[L.kind] || KIND_COLOR.semantic);
      colors[i * 6] = c.r;
      colors[i * 6 + 1] = c.g;
      colors[i * 6 + 2] = c.b;
      colors[i * 6 + 3] = c.r;
      colors[i * 6 + 4] = c.g;
      colors[i * 6 + 5] = c.b;
    }
    const lgeo = new THREE.BufferGeometry();
    lgeo.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    lgeo.setAttribute("color", new THREE.BufferAttribute(colors, 3));
    const lmat = new THREE.LineBasicMaterial({
      vertexColors: true,
      transparent: true,
      opacity: 0.45,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    this._linkLines = new THREE.LineSegments(lgeo, lmat);
    this.linkGroup.add(this._linkLines);

    // synaptic particles on strong links
    const strong = this._linkIdx
      .map((L, i) => ({ L, i }))
      .filter((x) => x.L.w >= 0.35)
      .slice(0, 120);
    if (strong.length) {
      const ppos = new Float32Array(strong.length * 3);
      const pgeo = new THREE.BufferGeometry();
      pgeo.setAttribute("position", new THREE.BufferAttribute(ppos, 3));
      const pmat = new THREE.PointsMaterial({
        color: 0xa5f3fc,
        size: 0.12,
        transparent: true,
        opacity: 0.9,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      });
      this._particles = new THREE.Points(pgeo, pmat);
      this.particleGroup.add(this._particles);
      this._particleData = strong.map((x) => ({
        li: x.i,
        t: Math.random(),
        speed: 0.25 + Math.random() * 0.45,
      }));
    } else {
      this._particles = null;
    }

    this._applyVisuals();
    this._syncPositions();
  }

  _stepForce(dt) {
    const n = this._sim.length;
    if (n < 2) return;
    const alpha = this._alpha;
    if (alpha < 0.01) return;

    // repulsion (Barnes-Hut lite: sample stride for large n)
    const stride = n > 250 ? 2 : 1;
    const repulse = 18 * alpha;
    for (let i = 0; i < n; i += stride) {
      for (let j = i + stride; j < n; j += stride) {
        const a = this._sim[i];
        const b = this._sim[j];
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let dz = a.z - b.z;
        let d2 = dx * dx + dy * dy + dz * dz + 0.15;
        const f = repulse / d2;
        const inv = 1 / Math.sqrt(d2);
        dx *= inv * f;
        dy *= inv * f;
        dz *= inv * f;
        a.vx += dx;
        a.vy += dy;
        a.vz += dz;
        b.vx -= dx;
        b.vy -= dy;
        b.vz -= dz;
      }
    }

    // springs
    const spring = 0.08 * alpha;
    for (const L of this._linkIdx) {
      const a = this._sim[L.s];
      const b = this._sim[L.t];
      if (!a || !b) continue;
      let dx = b.x - a.x;
      let dy = b.y - a.y;
      let dz = b.z - a.z;
      const dist = Math.sqrt(dx * dx + dy * dy + dz * dz) + 1e-4;
      const ideal = 2.2 + (1 - L.w) * 3.5;
      const f = spring * L.w * (dist - ideal);
      dx = (dx / dist) * f;
      dy = (dy / dist) * f;
      dz = (dz / dist) * f;
      a.vx += dx;
      a.vy += dy;
      a.vz += dz;
      b.vx -= dx;
      b.vy -= dy;
      b.vz -= dz;
    }

    // center gravity
    const g = 0.02 * alpha;
    for (const s of this._sim) {
      s.vx -= s.x * g;
      s.vy -= s.y * g;
      s.vz -= s.z * g;
      s.x += s.vx * dt;
      s.y += s.vy * dt;
      s.z += s.vz * dt;
      s.vx *= 0.86;
      s.vy *= 0.86;
      s.vz *= 0.86;
    }
    this._alpha *= 0.992;
  }

  _syncPositions() {
    for (let i = 0; i < this._nodeMeshes.length; i++) {
      const s = this._sim[i];
      if (!s) continue;
      this._nodeMeshes[i].position.set(s.x, s.y, s.z);
    }
    if (this._linkLines) {
      const pos = this._linkLines.geometry.getAttribute("position");
      for (let i = 0; i < this._linkIdx.length; i++) {
        const L = this._linkIdx[i];
        const a = this._sim[L.s];
        const b = this._sim[L.t];
        if (!a || !b) continue;
        pos.setXYZ(i * 2, a.x, a.y, a.z);
        pos.setXYZ(i * 2 + 1, b.x, b.y, b.z);
      }
      pos.needsUpdate = true;
    }
  }

  _stepParticles(dt) {
    if (!this._particles || !this._particleData.length) return;
    const pos = this._particles.geometry.getAttribute("position");
    for (let i = 0; i < this._particleData.length; i++) {
      const p = this._particleData[i];
      p.t = (p.t + p.speed * dt * 0.35) % 1;
      const L = this._linkIdx[p.li];
      if (!L) continue;
      const a = this._sim[L.s];
      const b = this._sim[L.t];
      if (!a || !b) continue;
      const t = p.t;
      pos.setXYZ(i, a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t, a.z + (b.z - a.z) * t);
    }
    pos.needsUpdate = true;
  }

  _applyVisuals() {
    const hasHits = this.hitSet.size > 0;
    const flash = performance.now() < this._flashUntil;
    for (let i = 0; i < this._nodeMeshes.length; i++) {
      const mesh = this._nodeMeshes[i];
      const node = this.nodes[i];
      if (!mesh || !node) continue;
      const lobeOk = this.enabledLobes.size === 0 || this.enabledLobes.has(node.lobe);
      let symOk = true;
      if (this.enabledSymbols && this.enabledSymbols.size > 0) {
        const s = (node.symbol || "").split("/")[0] || "";
        symOk = this.enabledSymbols.has(s) || this.enabledSymbols.has(node.symbol);
      }
      const visible = lobeOk && symOk;
      const isHit = this.hitSet.has(i);
      const isSel = i === this._selected;
      const isHov = i === this._hovered;
      mesh.visible = visible;
      if (!visible) continue;
      let em = 0.45;
      let op = 0.92;
      let sc = mesh.scale.x;
      const baseR = 0.18 + Math.min(0.55, (node.degree || 1) * 0.035);
      if (hasHits && !isHit) {
        em = 0.08;
        op = 0.22;
      }
      if (isHit) {
        em = flash ? 1.4 : 1.0;
        op = 1;
      }
      if (isSel || isHov) em = Math.max(em, 1.2);
      mesh.material.emissiveIntensity = em;
      mesh.material.opacity = op;
      const pulse = isHit && flash ? 1 + 0.25 * Math.sin(performance.now() * 0.012 + i) : 1;
      mesh.scale.setScalar(baseR * pulse * (isSel ? 1.35 : 1));
    }
    if (this._linkLines) {
      this._linkLines.material.opacity = hasHits ? 0.18 : 0.42;
    }
  }

  _tick(now) {
    this._raf = requestAnimationFrame(this._tick);
    if (!this._visible) {
      return;
    }
    const dt = 0.016;
    this._stepForce(dt);
    this._syncPositions();
    this._stepParticles(dt);
    if (now < this._flashUntil) this._applyVisuals();
    else if (this.hitSet.size && now > this._flashUntil + 50) {
      // keep query hits; only auto-clear pure flash sets via timeout in flashNew
    }
    if (now - this._idleSince > 9000) {
      this.controls.autoRotate = true;
      this.controls.autoRotateSpeed = 0.35;
    } else {
      this.controls.autoRotate = false;
    }
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  }

  _resize() {
    if (!this._visible) return;
    const w = window.innerWidth;
    const h = window.innerHeight;
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h, false);
  }

  _addBackdrop() {
    const g = new THREE.BufferGeometry();
    const n = 600;
    const pos = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) {
      pos[i * 3] = (Math.random() - 0.5) * 80;
      pos[i * 3 + 1] = (Math.random() - 0.5) * 80;
      pos[i * 3 + 2] = (Math.random() - 0.5) * 80;
    }
    g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    this.scene.add(
      new THREE.Points(
        g,
        new THREE.PointsMaterial({
          color: 0x1e293b,
          size: 0.04,
          transparent: true,
          opacity: 0.4,
          depthWrite: false,
        })
      )
    );
  }

  _onPointer(e) {
    if (!this._visible) return;
    const rect = this.canvas.getBoundingClientRect();
    this._pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    this._pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
    this.raycaster.setFromCamera(this._pointer, this.camera);
    const hits = this.raycaster.intersectObjects(this._nodeMeshes, false);
    if (hits.length) {
      const idx = hits[0].object.userData.index;
      this.selectIndex(idx);
      if (this._onPick) this._onPick(idx, this.nodes[idx]);
    }
  }

  _onMove(e) {
    if (!this._visible) return;
    const rect = this.canvas.getBoundingClientRect();
    this._pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    this._pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
    this.raycaster.setFromCamera(this._pointer, this.camera);
    const hits = this.raycaster.intersectObjects(this._nodeMeshes, false);
    const idx = hits.length ? hits[0].object.userData.index : -1;
    if (idx !== this._hovered) {
      this._hovered = idx;
      this._applyVisuals();
      if (this._onHover) this._onHover(idx, idx >= 0 ? this.nodes[idx] : null);
    }
  }

  _flyTo(x, y, z) {
    this.controls.target.set(x, y, z);
    const dir = new THREE.Vector3()
      .subVectors(this.camera.position, this.controls.target)
      .normalize()
      .multiplyScalar(10);
    this.camera.position.set(x + dir.x, y + dir.y + 2, z + dir.z);
    this.controls.update();
    this._idleSince = performance.now();
  }
}
