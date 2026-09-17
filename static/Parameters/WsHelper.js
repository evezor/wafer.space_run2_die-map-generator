class WsHelper extends GuiParameter {
  constructor(param, div) {
    super(param, div);
    this.pid = param.pid;
    this.svg = document.getElementById(`${param.pid}_die`);
    this.heatmapSvg = document.getElementById(`${param.pid}_heatmap`);
    this.button = document.getElementById(`${param.pid}_button`);
    this.gridContainer = document.getElementById(`${param.pid}_reticle_grid`);
    this.outputTextarea = document.getElementById(`${param.pid}_reticle_output`);
    this.copyBtn = document.getElementById(`${param.pid}_reticle_copy`);
    this.downloadBtn = document.getElementById(`${param.pid}_download`);
    this.reticleState = {}; // {`${ix},${iy}`: bool}
  }

  getHTML(param) {
    return `{{ html }}`
  }

  call(data) {
    data = JSON.parse(data);
    const type = data.cmd;
    console.log("WsHelper call type:", type);
    if (type === "die") {
      this.updateDieSVG(data.data);
    } else if (type === "heatmap") {
      this.updateHeatmapSVG(data.data);
    } else if (type === "reticle_grid") {
      this.renderReticleGrid(data.data);
    }
  }

  plotWafer() {
    console.log("Plotting wafer for pid:", this.pid);
    this.button.innerText = "Plotting...\nPlease wait\n";
    this.button.disabled = true;
    hermes.send(
      this.pid,
      JSON.stringify({ cmd: "plot_wafer" })
    );
  }

  renderReticleGrid(gridData) {
    this.gridContainer.innerHTML = '';
    this.reticleState = {};

    // Find grid extents
    let minIx = Infinity, maxIx = -Infinity;
    let minIy = Infinity, maxIy = -Infinity;
    for (const item of gridData) {
      if (item.ix < minIx) minIx = item.ix;
      if (item.ix > maxIx) maxIx = item.ix;
      if (item.iy < minIy) minIy = item.iy;
      if (item.iy > maxIy) maxIy = item.iy;
      this.reticleState[`${item.ix},${item.iy}`] = item.enabled;
    }

    const table = document.createElement('table');
    table.style.borderCollapse = 'collapse';

    // Render top-to-bottom so highest iy is at top (wafer Y convention)
    for (let iy = maxIy; iy >= minIy; iy--) {
      const tr = document.createElement('tr');
      for (let ix = minIx; ix <= maxIx; ix++) {
        const td = document.createElement('td');
        td.style.padding = '1px';
        td.style.textAlign = 'center';
        const key = `${ix},${iy}`;
        if (key in this.reticleState) {
          const cb = document.createElement('input');
          cb.type = 'checkbox';
          cb.checked = this.reticleState[key];
          cb.title = `(${ix}, ${iy})`;
          cb.style.cursor = 'pointer';
          cb.addEventListener('change', () => {
            this.reticleState[key] = cb.checked;
            this.updateOutput();
          });
          td.appendChild(cb);
        }
        tr.appendChild(td);
      }
      table.appendChild(tr);
    }

    this.gridContainer.appendChild(table);
    this.updateOutput();
  }

  // Build the ignore-set as a JSON array of [ix,iy] pairs (unchecked reticles).
  // This is the schema the wired config Variable expects (ignore_reticles), so
  // it can be pasted straight in - no Python `set` string, no source edit.
  updateOutput() {
    const ignored = [];
    for (const [key, enabled] of Object.entries(this.reticleState)) {
      if (!enabled) {
        const [ix, iy] = key.split(',').map(Number);
        ignored.push([ix, iy]);
      }
    }
    this.outputTextarea.value = JSON.stringify(ignored);
  }

  copyIgnoreJSON() {
    const text = this.outputTextarea.value;
    const done = () => {
      const prev = this.copyBtn.innerText;
      this.copyBtn.innerText = "Copied!";
      setTimeout(() => { this.copyBtn.innerText = prev; }, 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(() => {
        this.outputTextarea.select();
        document.execCommand('copy');
        done();
      });
    } else {
      this.outputTextarea.select();
      document.execCommand('copy');
      done();
    }
  }

  downloadSVG() {
    const svgEl = this.svg.querySelector('svg');
    if (!svgEl) return;
    const svgData = new XMLSerializer().serializeToString(svgEl);
    const blob = new Blob([svgData], { type: 'image/svg+xml' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'wafer_map.svg';
    a.click();
    URL.revokeObjectURL(url);
  }

  updateDieSVG(svgContent) {
    console.log("Updating Die SVG");
    this.svg.innerHTML = svgContent;
    this.downloadBtn.style.display = 'inline-block';

    this.die_svg = this.svg.querySelector('svg');

    this.die_svg.addEventListener("wheel", (e) => {
      e.preventDefault();
      let [x, y, width, height] = this.die_svg.getAttribute('viewBox').split(' ').map(Number);
      const scaleFactor = 1.1;
      const scale = e.deltaY < 0 ? 1 / scaleFactor : scaleFactor;
      const width2 = width * scale;
      const height2 = height * scale;
      this.die_svg.setAttribute('viewBox', `${x} ${y} ${width2} ${height2}`);
    });

    this.die_isDragging = false;
    this.die_lastX, this.die_lastY;

    this.die_svg.addEventListener("mousedown", (e) => {
      this.die_isDragging = true;
      this.die_lastX = e.clientX;
      this.die_lastY = e.clientY;
    });

    this.die_svg.addEventListener("mousemove", (e) => {
      if (!this.die_isDragging) return;
      const dx = e.clientX - this.die_lastX;
      const dy = e.clientY - this.die_lastY;
      let [x, y, width, height] = this.die_svg.getAttribute('viewBox').split(' ').map(Number);
      this.die_svg.setAttribute('viewBox', `${x - dx} ${y - dy} ${width} ${height}`);
      this.die_lastX = e.clientX;
      this.die_lastY = e.clientY;
    });

    this.die_svg.addEventListener("mouseup", () => {
      this.die_isDragging = false;
    });
  }

  updateHeatmapSVG(svgContent) {
    this.heatmapSvg.innerHTML = svgContent;
    this.hm_svg = this.heatmapSvg.querySelector('svg');

    this.hm_svg.addEventListener("wheel", (e) => {
      e.preventDefault();
      let [x, y, width, height] = this.hm_svg.getAttribute('viewBox').split(' ').map(Number);
      const scaleFactor = 1.1;
      const scale = e.deltaY < 0 ? 1 / scaleFactor : scaleFactor;
      const width2 = width * scale;
      const height2 = height * scale;
      this.hm_svg.setAttribute('viewBox', `${x} ${y} ${width2} ${height2}`);
    });

    this.hm_isDragging = false;
    this.hm_lastX, this.hm_lastY;

    this.hm_svg.addEventListener("mousedown", (e) => {
      this.hm_isDragging = true;
      this.hm_lastX = e.clientX;
      this.hm_lastY = e.clientY;
    });

    this.hm_svg.addEventListener("mousemove", (e) => {
      if (!this.hm_isDragging) return;
      const dx = e.clientX - this.hm_lastX;
      const dy = e.clientY - this.hm_lastY;
      let [x, y, width, height] = this.hm_svg.getAttribute('viewBox').split(' ').map(Number);
      this.hm_svg.setAttribute('viewBox', `${x - dx} ${y - dy} ${width} ${height}`);
      this.hm_lastX = e.clientX;
      this.hm_lastY = e.clientY;
    });

    this.hm_svg.addEventListener("mouseup", () => {
      this.hm_isDragging = false;
    });
  }
}
