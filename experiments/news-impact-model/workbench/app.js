const state = {
  profile: null,
  optimization: null,
  prediction: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || "Request failed");
  }
  return payload;
}

function pct(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  return `${(Number(value) * 100).toFixed(2)}%`;
}

function num(value, digits = 4) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  return Number(value).toFixed(digits);
}

function toast(message) {
  const node = $("toast");
  node.textContent = message;
  node.classList.add("show");
  window.setTimeout(() => node.classList.remove("show"), 2600);
}

function renderStatus() {
  const loaded = state.profile ? `${state.profile.event_count} events` : "not loaded";
  const mode = state.optimization
    ? `${state.optimization.mode} ${state.optimization.horizon}`
    : "idle";
  $("statusStrip").innerHTML = `
    <span>Dataset: ${loaded}</span>
    <span>Mode: ${mode}</span>
    <span>Leakage guard: walk-forward ready</span>
  `;
}

function renderProfile(profile) {
  state.profile = profile;
  $("datasetMetrics").innerHTML = `
    <div><span>Events</span><strong>${profile.event_count}</strong></div>
    <div><span>Horizons</span><strong>${profile.horizons.join(", ")}</strong></div>
    <div><span>Sources</span><strong>${Object.keys(profile.sources).length}</strong></div>
    <div><span>Types</span><strong>${Object.keys(profile.event_types).length}</strong></div>
  `;
  $("datasetBreakdown").innerHTML = [
    compactBars("Sources", profile.sources),
    compactBars("Types", profile.event_types),
    compactBars("Symbols", profile.symbols),
  ].join("");
  $("horizon").innerHTML = profile.horizons
    .map((horizon) => `<option>${horizon}</option>`)
    .join("");
  renderStatus();
}

function compactBars(title, counts) {
  const max = Math.max(1, ...Object.values(counts || {}));
  const rows = Object.entries(counts || {})
    .slice(0, 5)
    .map(([label, count]) => {
      const width = Math.max(4, (count / max) * 100);
      return `
        <div>
          <span>${label}</span>
          <span class="bar-track"><span class="bar-fill" style="width:${width}%"></span></span>
          <strong>${count}</strong>
        </div>
      `;
    })
    .join("");
  return `<section aria-label="${title}">${rows}</section>`;
}

function renderOptimization(optimization) {
  state.optimization = optimization;
  $("optimizerMetrics").innerHTML = `
    <div><span>MAE</span><strong>${num(optimization.metrics.mae, 5)}</strong></div>
    <div><span>Direction</span><strong>${pct(optimization.metrics.directional_accuracy)}</strong></div>
    <div><span>Predictions</span><strong>${optimization.n_predictions}</strong></div>
    <div><span>Candidates</span><strong>${optimization.candidates_tested}</strong></div>
  `;
  const entries = Object.entries(optimization.weights);
  const max = Math.max(1, ...entries.map(([, value]) => Number(value)));
  $("weights").innerHTML = entries
    .map(([label, value]) => {
      const width = Math.max(4, (Number(value) / max) * 100);
      return `
        <div class="weight-row">
          <span>${label}</span>
          <span class="bar-track"><span class="bar-fill" style="width:${width}%"></span></span>
          <strong>${Number(value).toFixed(2)}</strong>
        </div>
      `;
    })
    .join("");
  $("exportBox").value = JSON.stringify(optimization.weights, null, 2);
  renderStatus();
}

function renderPrediction(prediction) {
  state.prediction = prediction;
  $("modelVersion").textContent = prediction.model_version;
  $("confidence").textContent = `Confidence ${pct(prediction.confidence)}`;
  $("impactGrid").innerHTML = Object.entries(prediction.horizons)
    .map(([horizon, impact]) => {
      const direction = impact.expected_return >= 0 ? "positive" : "negative";
      return `
        <article class="impact-card">
          <div class="impact-top">
            <strong>${horizon}</strong>
            <span class="impact-value ${direction}">${pct(impact.expected_return)}</span>
          </div>
          <div class="impact-range">
            <span>q10 ${pct(impact.q10)}</span>
            <span>q50 ${pct(impact.q50)}</span>
            <span>q90 ${pct(impact.q90)}</span>
          </div>
          <div class="impact-range">
            <span>p up ${pct(impact.p_up)}</span>
            <span>sample ${impact.sample_size}</span>
            <span>vol ${num(prediction.volatility_impact, 3)}</span>
          </div>
        </article>
      `;
    })
    .join("");
  $("evidenceRows").innerHTML = prediction.similar_events
    .map((event) => `
      <tr>
        <td>${num(event.score, 3)}</td>
        <td>${event.source}</td>
        <td>${event.event_type}</td>
        <td>${event.headline}</td>
        <td>${pct(event.abnormal_returns["5m"])}</td>
        <td>${pct(event.abnormal_returns["30m"])}</td>
      </tr>
    `)
    .join("");
}

function currentHorizons() {
  if (state.profile?.horizons?.length) {
    return state.profile.horizons;
  }
  return ["5m", "30m", "1h"];
}

async function refreshStatus() {
  const payload = await api("/api/status");
  if (payload.profile) {
    renderProfile(payload.profile);
  }
  if (payload.last_optimization) {
    renderOptimization(payload.last_optimization);
  }
  renderStatus();
}

$("datasetForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const payload = await api("/api/dataset/load", {
      method: "POST",
      body: JSON.stringify({ path: $("datasetPath").value.trim() }),
    });
    renderProfile(payload.profile);
    toast("Dataset loaded");
  } catch (error) {
    toast(error.message);
  }
});

$("optimizeForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const payload = await api("/api/optimize", {
      method: "POST",
      body: JSON.stringify({
        horizon: $("horizon").value,
        mode: $("mode").value,
        min_train_events: Number($("minTrainEvents").value),
        top_k: Number($("topK").value),
      }),
    });
    renderOptimization(payload.optimization);
    toast("Weights optimized");
  } catch (error) {
    toast(error.message);
  }
});

$("predictForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const nowNs = Date.now() * 1_000_000;
    const payload = await api("/api/predict", {
      method: "POST",
      body: JSON.stringify({
        horizons: currentHorizons(),
        top_k: Number($("topK").value),
        event: {
          event_id: "manual-live-event",
          available_at_ns: nowNs,
          source: $("source").value.trim(),
          headline: $("headline").value.trim(),
          body: $("body").value.trim(),
          symbols: [$("symbol").value.trim()],
          event_type: $("eventType").value,
        },
        context: {
          symbol: $("symbol").value.trim(),
          market_regime: $("marketRegime").value,
        },
      }),
    });
    renderPrediction(payload.prediction);
    toast("Prediction updated");
  } catch (error) {
    toast(error.message);
  }
});

$("copyWeights").addEventListener("click", async () => {
  const text = $("exportBox").value;
  if (!text) {
    toast("No weights to copy yet");
    return;
  }
  await navigator.clipboard.writeText(text);
  toast("Weights copied");
});

refreshStatus().catch((error) => toast(error.message));
