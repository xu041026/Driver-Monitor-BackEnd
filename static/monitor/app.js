"use strict";

(() => {
  const SVG_NS = "http://www.w3.org/2000/svg";
  const node = (name, attributes = {}, text = "") => {
    const element = document.createElementNS(SVG_NS, name);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
    if (text) element.textContent = text;
    return element;
  };

  for (const container of document.querySelectorAll("[data-trend-chart]")) {
    const source = document.getElementById(container.dataset.source);
    let data;
    try { data = JSON.parse(source.textContent); } catch { data = []; }
    if (!Array.isArray(data) || !data.length) {
      container.textContent = "暂无趋势数据，终端上报后将在这里显示。";
      continue;
    }
    data = data.map(point => ({label: String(point.label ?? ""), count: Math.max(0, Number(point.count) || 0)}));
    const width = 680, height = 190;
    const inset = {top: 14, right: 28, bottom: 44, left: 40};
    const plotWidth = width - inset.left - inset.right;
    const plotHeight = height - inset.top - inset.bottom;
    const maxCount = Math.max(...data.map(point => point.count));
    // Show 0–10 by default, expanding for larger counts without clipping data.
    const top = Math.max(10, Math.ceil(maxCount / 10) * 10);
    const tickCount = 5;
    const x = index => inset.left + (data.length === 1 ? plotWidth / 2 : index * plotWidth / (data.length - 1));
    const y = count => inset.top + plotHeight - count / top * plotHeight;
    const svg = node("svg", {viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none", "aria-hidden": "true"});
    for (let tick = 0; tick <= tickCount; tick++) {
      const count = top / tickCount * tick;
      svg.append(node("line", {x1: inset.left, y1: y(count), x2: width - inset.right, y2: y(count), stroke: "#edf2f4", "stroke-dasharray": tick ? "3 5" : "none"}));
      svg.append(node("text", {x: inset.left - 12, y: y(count) + 3, "text-anchor": "end", class: "chart-axis-label"}, String(count)));
    }
    const points = data.map((point, index) => `${x(index)},${y(point.count)}`).join(" ");
    if (maxCount > 0) {
      const area = `M ${x(0)},${y(0)} L ${points.split(" ").join(" L ")} L ${x(data.length - 1)},${y(0)} Z`;
      svg.append(node("path", {d: area, fill: "#148f83", "fill-opacity": ".07"}));
    }
    svg.append(node("polyline", {points, fill: "none", stroke: "#239887", "stroke-width": "2.2", "stroke-linecap": "round", "stroke-linejoin": "round"}));
    data.forEach((point, index) => {
      const marker = node("circle", {cx: x(index), cy: y(point.count), r: 3.2, fill: "#fff", stroke: "#239887", "stroke-width": "1.8"});
      marker.append(node("title", {}, `${point.label}：${point.count} 条告警`));
      svg.append(marker);
      if (data.length <= 10 || index % Math.ceil(data.length / 8) === 0 || index === data.length - 1) {
        svg.append(node("text", {x: x(index), y: y(0) + 22, "text-anchor": "middle", class: "chart-label"}, point.label));
      }
    });
    if (!maxCount) svg.append(node("text", {x: width / 2, y: inset.top + plotHeight / 2, "text-anchor": "middle", class: "chart-zero-label"}, "此时间段暂无告警"));
    container.replaceChildren(svg);
    container.setAttribute("aria-label", `每日告警数量：${data.map(point => `${point.label} ${point.count}条`).join("，")}`);
  }

  for (const output of document.querySelectorAll("[data-json-source]")) {
    const source = document.getElementById(output.dataset.jsonSource);
    try { output.textContent = JSON.stringify(JSON.parse(source.textContent), null, 2); } catch { /* Keep the server-rendered fallback. */ }
  }

  for (const form of document.querySelectorAll("form[data-confirm]")) {
    form.addEventListener("submit", event => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  }

  for (const button of document.querySelectorAll("[data-copy-target]")) {
    button.addEventListener("click", async () => {
      const target = document.getElementById(button.dataset.copyTarget);
      const feedback = document.getElementById("copy-feedback");
      if (!target) return;
      try {
        await navigator.clipboard.writeText(target.textContent.trim());
        if (feedback) feedback.textContent = "示例已复制。发送前请替换为真实事件信息。";
      } catch {
        const range = document.createRange();
        range.selectNodeContents(target);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        if (feedback) feedback.textContent = "已选中示例，请使用系统复制功能。";
      }
    });
  }
})();
