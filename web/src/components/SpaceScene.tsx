'use client';

import { useEffect, useRef } from 'react';
import * as THREE from 'three';

/* The landing's 3D background: stars, a drifting candlestick chart, a
 * wireframe planet, a few slow geometric shapes and the odd shooting star.
 *
 * This is the ONE place the app runs a continuous render loop, and it is
 * bounded: it runs only while `active` (the landing is on screen) and stops
 * outright once the app is entered -- the Data tab's no-rAF-loop rule is
 * about the working screens, and still holds there. It also pauses when the
 * browser tab is hidden and honours prefers-reduced-motion.
 *
 * Loaded with next/dynamic from Landing, so three.js is a separate chunk that
 * the rest of the app never downloads.
 *
 * Deliberately dark and quiet: low star opacity, dim candles, faint lines.
 * It is a backdrop for the wordmark, not the subject.
 */

type Props = { active: boolean; warp: boolean };

export default function SpaceScene({ active, warp }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const ctl = useRef<{ start: () => void; stop: () => void; warp: (on: boolean) => void } | null>(null);
  const activeRef = useRef(active);

  useEffect(() => {
    const canvas = canvasRef.current!;
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: 'high-performance' });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.75));
    renderer.setClearColor(0x020305, 1);
    const scene = new THREE.Scene();
    scene.fog = new THREE.FogExp2(0x020305, 0.013);
    const camera = new THREE.PerspectiveCamera(58, 1, 0.1, 900);
    const HOME = new THREE.Vector3(0, 3.2, 22);
    camera.position.copy(HOME);

    scene.add(new THREE.AmbientLight(0x2a3240, 0.7));
    const key = new THREE.DirectionalLight(0xbfd4ff, 0.9);
    key.position.set(12, 20, 10);
    scene.add(key);
    const rim = new THREE.PointLight(0x3f9e74, 18, 90, 1.6);
    rim.position.set(-18, 4, -18);
    scene.add(rim);
    const rim2 = new THREE.PointLight(0x7fb8ff, 12, 90, 1.6);
    rim2.position.set(22, 10, -30);
    scene.add(rim2);

    const disposables: { dispose: () => void }[] = [];
    const track = <T extends { dispose: () => void }>(x: T) => { disposables.push(x); return x; };

    // -- stars, three depth layers ----------------------------------------
    const dot = track(radialTexture());
    const stars: THREE.Points[] = [];
    ([[2200, 0.5, 0xffffff, 0.45], [700, 0.9, 0xcfe0ff, 0.5], [110, 1.8, 0xffffff, 0.6]] as const)
      .forEach(([n, size, color, op], k) => {
        const geo = track(new THREE.BufferGeometry());
        const pos = new Float32Array(n * 3);
        for (let i = 0; i < n; i++) {
          const r = 160 + Math.random() * 420;
          const th = Math.random() * Math.PI * 2;
          const ph = Math.acos(2 * Math.random() - 1);
          pos[i * 3] = r * Math.sin(ph) * Math.cos(th);
          pos[i * 3 + 1] = r * Math.cos(ph) * 0.7;
          pos[i * 3 + 2] = r * Math.sin(ph) * Math.sin(th) - 120;
        }
        geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
        const mat = track(new THREE.PointsMaterial({
          size, color, map: dot, transparent: true, opacity: op, depthWrite: false,
          sizeAttenuation: true, blending: THREE.AdditiveBlending, fog: false,
        }));
        const p = new THREE.Points(geo, mat);
        p.userData.spin = (k + 1) * 0.004;
        scene.add(p);
        stars.push(p);
      });

    const grid = new THREE.GridHelper(600, 120, 0x1f3040, 0x121a23);
    grid.position.y = -9;
    (grid.material as THREE.Material).transparent = true;
    (grid.material as THREE.Material).opacity = 0.22;
    scene.add(grid);

    // -- the candle river --------------------------------------------------
    const N = 110, GAP = 1.5, AMP = 2.1;
    const bodyMat = track(new THREE.MeshStandardMaterial({
      metalness: 0.35, roughness: 0.4, transparent: true, opacity: 0.42, emissive: 0x05080b,
    }));
    const wickMat = track(new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.3 }));
    const bodies = new THREE.InstancedMesh(track(new THREE.BoxGeometry(0.8, 1, 0.8)), bodyMat, N);
    const wicks = new THREE.InstancedMesh(track(new THREE.BoxGeometry(0.07, 1, 0.07)), wickMat, N);
    const river = new THREE.Group();
    river.add(bodies, wicks);
    river.position.set(0, -8.5, -30);
    river.rotation.set(0.05, -0.34, 0);
    scene.add(river);
    const UP = new THREE.Color(0x3f9e74), DOWN = new THREE.Color(0xc8635c);
    let price = 0, drift = 0;
    const next = () => {
      drift = drift * 0.9 + (Math.random() - 0.47) * 0.35;
      const o = price, c = o + drift + (Math.random() - 0.5) * 0.9;
      price = c;
      return { o, c, h: Math.max(o, c) + Math.random() * 0.7, l: Math.min(o, c) - Math.random() * 0.7 };
    };
    const candles = Array.from({ length: N }, (_, i) => ({ x: (i - N / 2) * GAP, ...next() }));
    const M = new THREE.Matrix4(), Q = new THREE.Quaternion(), S = new THREE.Vector3(), P = new THREE.Vector3();
    const layout = () => {
      const mean = candles.reduce((a, k) => a + k.c, 0) / N;
      candles.forEach((k, i) => {
        const y0 = k.o - mean, y1 = k.c - mean;
        P.set(k.x, ((y0 + y1) / 2) * AMP, 0);
        S.set(1, Math.max(Math.abs(y1 - y0) * AMP, 0.1), 1);
        bodies.setMatrixAt(i, M.compose(P, Q, S));
        P.set(k.x, ((k.h - mean + (k.l - mean)) / 2) * AMP, 0);
        S.set(1, (k.h - k.l) * AMP, 1);
        wicks.setMatrixAt(i, M.compose(P, Q, S));
        const col = k.c >= k.o ? UP : DOWN;
        bodies.setColorAt(i, col);
        wicks.setColorAt(i, col);
      });
      bodies.instanceMatrix.needsUpdate = wicks.instanceMatrix.needsUpdate = true;
      bodies.instanceColor!.needsUpdate = wicks.instanceColor!.needsUpdate = true;
    };
    layout();

    // -- planet, rings and floating shapes ------------------------------------
    const planetAt = new THREE.Vector3(-128, 52, -200);
    const planet = new THREE.Mesh(track(new THREE.IcosahedronGeometry(16, 2)),
      track(new THREE.MeshBasicMaterial({ color: 0x2e4052, wireframe: true, transparent: true, opacity: 0.14, fog: false })));
    planet.position.copy(planetAt);
    const core = new THREE.Mesh(track(new THREE.SphereGeometry(15.4, 48, 48)),
      track(new THREE.MeshStandardMaterial({ color: 0x0b1118, roughness: 0.9, metalness: 0.1, emissive: 0x05080c, fog: false })));
    core.position.copy(planetAt);
    const ring = new THREE.Mesh(track(new THREE.TorusGeometry(26, 0.08, 8, 180)),
      track(new THREE.MeshBasicMaterial({ color: 0x3f9e74, transparent: true, opacity: 0.18, fog: false })));
    ring.position.copy(planetAt);
    ring.rotation.set(1.25, 0.2, 0.3);
    const ring2 = new THREE.Mesh(track(new THREE.TorusGeometry(31, 0.04, 8, 200)),
      track(new THREE.MeshBasicMaterial({ color: 0x7fb8ff, transparent: true, opacity: 0.09, fog: false })));
    ring2.position.copy(planetAt);
    ring2.rotation.set(1.3, 0.1, 0.25);
    scene.add(planet, core, ring, ring2);

    const shapes = [new THREE.OctahedronGeometry(1.4), new THREE.TetrahedronGeometry(1.5),
      new THREE.IcosahedronGeometry(1.2), new THREE.TorusKnotGeometry(0.9, 0.22, 90, 10)];
    const floaters: THREE.LineSegments[] = [];
    for (let i = 0; i < 9; i++) {
      const edges = track(new THREE.EdgesGeometry(shapes[i % shapes.length]));
      const e = new THREE.LineSegments(edges, track(new THREE.LineBasicMaterial({
        color: i % 3 === 0 ? 0x3f9e74 : 0x9fb3c8, transparent: true, opacity: 0.22,
      })));
      const side = i % 2 ? 1 : -1;
      e.position.set(side * (40 + Math.random() * 24), 2 + Math.random() * 16, -14 - Math.random() * 30);
      e.userData = { bob: Math.random() * 6.28, sp: 0.2 + Math.random() * 0.4,
        rx: (Math.random() - 0.5) * 0.01, ry: (Math.random() - 0.5) * 0.012, y: e.position.y };
      scene.add(e);
      floaters.push(e);
    }
    shapes.forEach((g) => g.dispose());

    // -- shooting stars ---------------------------------------------------------
    const streakTex = track(streakTexture());
    const streakGeo = track(new THREE.PlaneGeometry(14, 0.12));
    const streaks = Array.from({ length: 5 }, () => {
      const m = new THREE.Mesh(streakGeo, track(new THREE.MeshBasicMaterial({
        map: streakTex, transparent: true, opacity: 0, depthWrite: false,
        blending: THREE.AdditiveBlending, fog: false,
      })));
      m.visible = false;
      scene.add(m);
      return { m, t: 0, life: 0, v: new THREE.Vector3() };
    });
    let nextStreak = 1.2;
    const fire = () => {
      const s = streaks.find((x) => !x.m.visible);
      if (!s) return;
      s.m.position.set((Math.random() - 0.3) * 120, 25 + Math.random() * 30, -60 - Math.random() * 80);
      const ang = -0.35 - Math.random() * 0.35;
      s.v.set(Math.cos(ang), Math.sin(ang), 0).multiplyScalar(-70 - Math.random() * 40);
      s.m.rotation.z = Math.atan2(s.v.y, s.v.x);
      s.t = 0;
      s.life = 0.9 + Math.random() * 0.6;
      s.m.visible = true;
    };

    // -- loop ---------------------------------------------------------------------
    const mouse = new THREE.Vector2();
    const target = new THREE.Vector3();
    const onMove = (e: PointerEvent) => mouse.set(e.clientX / innerWidth - 0.5, e.clientY / innerHeight - 0.5);
    const resize = () => {
      renderer.setSize(innerWidth, innerHeight, false);
      camera.aspect = innerWidth / innerHeight;
      camera.updateProjectionMatrix();
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('resize', resize);
    resize();

    let raf = 0, running = false, last = 0, warpNow = 0, warpTo = 0;
    const frame = (now: number) => {
      if (!running) return;
      const dt = Math.min((now - last) / 1000, 0.05);
      last = now;
      const t = now / 1000;
      warpNow += (warpTo - warpNow) * Math.min(1, dt * 2.4);
      target.set(HOME.x + mouse.x * 4, HOME.y - mouse.y * 2.2, HOME.z - warpNow * 34);
      camera.position.lerp(target, Math.min(1, dt * 2));
      camera.fov = 58 + warpNow * 22;
      camera.updateProjectionMatrix();
      camera.lookAt(0, 0, -40);

      stars.forEach((p) => { p.rotation.y += p.userData.spin * dt * (1 + warpNow * 20); });
      planet.rotation.y += dt * 0.05;
      planet.rotation.x += dt * 0.01;
      ring.rotation.z += dt * 0.03;
      ring2.rotation.z -= dt * 0.02;
      floaters.forEach((f) => {
        f.rotation.x += f.userData.rx;
        f.rotation.y += f.userData.ry;
        f.position.y = f.userData.y + Math.sin(t * f.userData.sp + f.userData.bob) * 0.8;
      });
      if (!reduce) {
        for (const k of candles) k.x -= dt * 1.4;
        if (candles[0].x < (-N / 2) * GAP) {
          candles.shift();
          candles.push({ x: candles[candles.length - 1].x + GAP, ...next() });
        }
        layout();
        nextStreak -= dt;
        if (nextStreak <= 0) { fire(); nextStreak = 1.6 + Math.random() * 3.8; }
      }
      bodyMat.opacity = 0.42 * (1 - warpNow * 0.6);
      for (const s of streaks) {
        if (!s.m.visible) continue;
        s.t += dt;
        const k = s.t / s.life;
        s.m.position.addScaledVector(s.v, dt);
        (s.m.material as THREE.MeshBasicMaterial).opacity = Math.sin(Math.min(k, 1) * Math.PI) * 0.55;
        if (k >= 1) s.m.visible = false;
      }
      renderer.render(scene, camera);
      raf = requestAnimationFrame(frame);
    };
    const start = () => {
      if (running || document.hidden) return;
      running = true;
      last = performance.now();
      raf = requestAnimationFrame(frame);
    };
    const stop = () => { running = false; cancelAnimationFrame(raf); };
    const onVis = () => (document.hidden ? stop() : ctl.current && activeRef.current && start());
    document.addEventListener('visibilitychange', onVis);
    ctl.current = { start, stop, warp: (on) => { warpTo = on ? 1 : 0; } };
    if (activeRef.current) start();

    return () => {
      stop();
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('resize', resize);
      document.removeEventListener('visibilitychange', onVis);
      disposables.forEach((d) => d.dispose());
      bodies.dispose();
      wicks.dispose();
      renderer.dispose();
      ctl.current = null;
    };
  }, []);

  // Props drive the loop without recreating the scene.
  useEffect(() => {
    activeRef.current = active;
    if (active) ctl.current?.start();
    else {
      // Let the warp finish playing before the loop stops.
      const t = window.setTimeout(() => ctl.current?.stop(), 1300);
      return () => window.clearTimeout(t);
    }
  }, [active]);
  useEffect(() => { ctl.current?.warp(warp); }, [warp]);

  return <canvas ref={canvasRef} className="space-canvas" aria-hidden="true" />;
}

function radialTexture(): THREE.CanvasTexture {
  const c = document.createElement('canvas');
  c.width = c.height = 64;
  const g = c.getContext('2d')!;
  const r = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  r.addColorStop(0, 'rgba(255,255,255,1)');
  r.addColorStop(0.25, 'rgba(255,255,255,.8)');
  r.addColorStop(1, 'rgba(255,255,255,0)');
  g.fillStyle = r;
  g.fillRect(0, 0, 64, 64);
  return new THREE.CanvasTexture(c);
}

function streakTexture(): THREE.CanvasTexture {
  const c = document.createElement('canvas');
  c.width = 256;
  c.height = 8;
  const g = c.getContext('2d')!;
  const gr = g.createLinearGradient(0, 0, 256, 0);
  gr.addColorStop(0, 'rgba(255,255,255,0)');
  gr.addColorStop(0.85, 'rgba(200,225,255,.7)');
  gr.addColorStop(1, 'rgba(255,255,255,1)');
  g.fillStyle = gr;
  g.fillRect(0, 2, 256, 4);
  return new THREE.CanvasTexture(c);
}
