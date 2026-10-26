/* WONTECH 세계지도 (대륙/국가 마우스오버)
 * 필요: d3@7, topojson-client@3 (페이지에서 먼저 로드)
 * 사용: WorldMap.render(element, {mode, regionFill, countryFill, regionTip, countryTip})
 * 지역 구분은 Grand View Research 기준 (멕시코 = 북미, 터키 = 유럽) */
(function () {
  const TOPO_URL = 'https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/countries-110m.json';

  const REGION_NAMES = { NA: '북미', EU: '유럽', APAC: '아시아태평양', LATAM: '중남미', MEA: '중동·아프리카' };
  const LIST = {
    NA: [840, 124, 484, 304],
    LATAM: [32, 68, 76, 152, 170, 188, 192, 214, 218, 222, 320, 332, 340, 388, 558, 591, 600, 604, 630, 740, 780, 858, 862, 328, 44, 84, 238],
    EU: [8, 40, 56, 70, 100, 112, 191, 196, 203, 208, 233, 246, 250, 276, 300, 348, 352, 372, 380, 428, 440, 442, 498, 499, 528, 807, 578, 616, 620, 642, 643, 688, 703, 705, 724, 752, 756, 792, 804, 826],
    MEA: [12, 24, 72, 108, 120, 140, 148, 178, 180, 204, 226, 231, 232, 262, 266, 270, 288, 324, 384, 404, 426, 430, 434, 450, 454, 466, 478, 504, 508, 516, 562, 566, 624, 646, 686, 694, 706, 710, 728, 729, 732, 748, 768, 788, 800, 818, 834, 854, 894, 716,
          364, 368, 376, 400, 414, 422, 512, 634, 682, 760, 784, 887, 275]
  };
  const BY_NAME = { 'Kosovo': 'EU', 'N. Cyprus': 'EU', 'Somaliland': 'MEA' };
  const regionOf = (id, name) => {
    const n = +id;
    for (const k of ['NA', 'LATAM', 'EU', 'MEA']) if (LIST[k].includes(n)) return k;
    return BY_NAME[name] || 'APAC';
  };
  const pad = id => String(id).padStart(3, '0');

  let topoCache = null;
  async function loadTopo() {
    if (!topoCache) topoCache = fetch(TOPO_URL).then(r => r.json());
    return topoCache;
  }

  function css() {
    if (document.getElementById('wm-style')) return;
    const s = document.createElement('style');
    s.id = 'wm-style';
    s.textContent = `
.wm-wrap{position:relative;width:100%}
.wm-wrap svg{width:100%;height:auto;display:block;overflow:visible}
.wm-unit{transition:transform .18s ease, filter .18s ease;transform-box:fill-box;transform-origin:center;cursor:pointer}
.wm-unit path{stroke:var(--wm-stroke,#fff);stroke-width:.5;vector-effect:non-scaling-stroke;transition:fill .18s ease}
.wm-unit.on{transform:translateY(-4px) scale(1.03);filter:drop-shadow(0 6px 6px rgba(0,0,0,.28))}
.wm-unit.dim{opacity:.55}
.wm-tip{position:absolute;pointer-events:none;background:var(--wm-tip-bg,#171717);color:var(--wm-tip-fg,#fff);font-size:12px;line-height:1.55;padding:9px 11px;border-radius:8px;box-shadow:0 6px 18px rgba(0,0,0,.18);max-width:240px;opacity:0;transition:opacity .12s;z-index:5;white-space:normal}
.wm-tip.show{opacity:1}
.wm-tip b{font-size:13px}
.wm-tip .r{display:flex;justify-content:space-between;gap:14px}
.wm-tip .r span:first-child{opacity:.75}
.wm-tip .n{opacity:.65;font-size:11px;margin-top:4px}`;
    document.head.appendChild(s);
  }

  async function render(el, opt) {
    css();
    const topo = await loadTopo();
    const feats = topojson.feature(topo, topo.objects.countries).features
      .filter(f => f.properties.name !== 'Antarctica');
    feats.forEach(f => { f.iso = pad(f.id); f.region = regionOf(f.id, f.properties.name); });

    const W = 960, H = 470;
    const proj = d3.geoNaturalEarth1().fitExtent([[4, 10], [W - 4, H - 4]], { type: 'FeatureCollection', features: feats });
    const path = d3.geoPath(proj);

    el.innerHTML = '';
    const wrap = d3.select(el).append('div').attr('class', 'wm-wrap');
    const svg = wrap.append('svg').attr('viewBox', `0 0 ${W} ${H}`).attr('role', 'img')
      .attr('aria-label', opt.ariaLabel || '세계지도');
    const tip = wrap.append('div').attr('class', 'wm-tip');
    const layer = svg.append('g');

    let units;
    if (opt.mode === 'country') {
      units = layer.selectAll('g').data(feats).join('g').attr('class', 'wm-unit');
      units.append('path').attr('d', path).style('fill', d => opt.countryFill(d.iso, d.region));
    } else {
      const groups = d3.groups(feats, f => f.region).map(([k, fs]) => ({ key: k, feats: fs }));
      units = layer.selectAll('g').data(groups).join('g').attr('class', 'wm-unit');
      units.selectAll('path').data(d => d.feats).join('path').attr('d', path)
        .style('fill', function () { return opt.regionFill(d3.select(this.parentNode).datum().key); });
    }

    const html = d => opt.mode === 'country'
      ? opt.countryTip(d.iso, d.region, d.properties.name)
      : opt.regionTip(d.key);

    function place(evt) {
      const box = wrap.node().getBoundingClientRect();
      const t = tip.node();
      let x = evt.clientX - box.left + 14, y = evt.clientY - box.top + 14;
      if (x + t.offsetWidth > box.width) x = evt.clientX - box.left - t.offsetWidth - 14;
      if (x < 0) x = 4;
      if (y + t.offsetHeight > box.height) y = Math.max(4, evt.clientY - box.top - t.offsetHeight - 14);
      tip.style('left', x + 'px').style('top', y + 'px');
    }
    function on(evt, d) {
      const h = html(d);
      units.classed('on', u => u === d).classed('dim', u => u !== d && !!h);
      this.parentNode.appendChild(this); // 맨 위로 올려 튀어나와 보이게
      if (h) { tip.html(h).classed('show', true); place(evt); } else tip.classed('show', false);
    }
    function off() { units.classed('on', false).classed('dim', false); tip.classed('show', false); }

    units.on('mouseenter', on).on('mousemove', place).on('mouseleave', off)
      .on('click', function (evt, d) { evt.stopPropagation(); on.call(this, evt, d); });
    d3.select(document).on('click.wm' + Math.random().toString(36).slice(2), off);
  }

  window.WorldMap = { render, REGION_NAMES, regionOf };
})();
