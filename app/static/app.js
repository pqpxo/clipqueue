// version 1.2.0
(() => {
  "use strict";

  const state = {
    videos: [],
    jobs: [],
    currentIndex: -1,
    currentVideo: null,
    duration: 0,
    previewingCut: false,
    refreshTimer: null,
  };

  const $ = (selector) => document.querySelector(selector);
  const elements = {
    gallery: $("#gallery"),
    emptyState: $("#emptyState"),
    gallerySummary: $("#gallerySummary"),
    heroStats: $("#heroStats"),
    showSkipped: $("#showSkipped"),
    rescanButton: $("#rescanButton"),
    queueButton: $("#queueButton"),
    queueCount: $("#queueCount"),
    queueDot: $("#queueDot"),
    queuePanel: $("#queuePanel"),
    queueBackdrop: $("#queueBackdrop"),
    closeQueueButton: $("#closeQueueButton"),
    clearQueueButton: $("#clearQueueButton"),
    queueJobs: $("#queueJobs"),
    editorModal: $("#editorModal"),
    closeEditorButton: $("#closeEditorButton"),
    editorPosition: $("#editorPosition"),
    editorTitle: $("#editorTitle"),
    editorPath: $("#editorPath"),
    editorVideo: $("#editorVideo"),
    previewUnavailable: $("#previewUnavailable"),
    playheadReadout: $("#playheadReadout"),
    setStartButton: $("#setStartButton"),
    setEndButton: $("#setEndButton"),
    cutStartRange: $("#cutStartRange"),
    cutEndRange: $("#cutEndRange"),
    cutStartInput: $("#cutStartInput"),
    cutEndInput: $("#cutEndInput"),
    timelineCut: $("#timelineCut"),
    durationLabel: $("#durationLabel"),
    removeSummary: $("#removeSummary"),
    removedLength: $("#removedLength"),
    previewCutButton: $("#previewCutButton"),
    deleteSourceOnSuccess: $("#deleteSourceOnSuccess"),
    previousButton: $("#previousButton"),
    skipButton: $("#skipButton"),
    queueCutButton: $("#queueCutButton"),
    videoCardTemplate: $("#videoCardTemplate"),
    queueJobTemplate: $("#queueJobTemplate"),
    toastArea: $("#toastArea"),
  };

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    if (!response.ok) {
      let message = "The request could not be completed.";
      try { message = (await response.json()).detail || message; } catch (_) { /* no JSON response */ }
      throw new Error(message);
    }
    return response.status === 204 ? null : response.json();
  }

  function formatTime(seconds) {
    const safe = Math.max(0, Number(seconds) || 0);
    const hours = Math.floor(safe / 3600);
    const minutes = Math.floor((safe % 3600) / 60);
    const whole = Math.floor(safe % 60);
    const decimal = Math.floor((safe % 1) * 10);
    const base = hours ? `${hours}:${String(minutes).padStart(2, "0")}:${String(whole).padStart(2, "0")}` : `${minutes}:${String(whole).padStart(2, "0")}`;
    return `${base}.${decimal}`;
  }

  function formatDuration(seconds) {
    const safe = Math.max(0, Number(seconds) || 0);
    const hours = Math.floor(safe / 3600);
    const minutes = Math.floor((safe % 3600) / 60);
    const whole = Math.floor(safe % 60);
    return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${String(whole).padStart(2, "0")}` : `${minutes}:${String(whole).padStart(2, "0")}`;
  }

  function formatBytes(bytes) {
    const value = Number(bytes) || 0;
    if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`;
    if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
    return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  }

  function showToast(message, kind = "") {
    const toast = document.createElement("div");
    toast.className = `toast ${kind}`;
    toast.textContent = message;
    elements.toastArea.append(toast);
    window.setTimeout(() => toast.remove(), 4200);
  }

  async function refreshAll({ quiet = false } = {}) {
    try {
      const [videosResponse, jobsResponse] = await Promise.all([
        api(`/api/videos?include_skipped=${elements.showSkipped.checked}`),
        api("/api/jobs"),
      ]);
      state.videos = videosResponse.videos;
      state.jobs = jobsResponse.jobs;
      renderGallery();
      renderQueue();
    } catch (error) {
      if (!quiet) showToast(error.message, "error");
    }
  }

  function renderGallery() {
    elements.gallery.innerHTML = "";
    elements.emptyState.classList.toggle("hidden", state.videos.length > 0);
    elements.gallerySummary.textContent = state.videos.length
      ? `${state.videos.length} video${state.videos.length === 1 ? "" : "s"} ready to review`
      : "No matching videos in the mounted input folder";

    const queuedVideoIds = new Map(
      state.jobs.filter((job) => ["queued", "processing", "complete"].includes(job.status)).map((job) => [job.video_id, job]),
    );
    let totalDuration = 0;
    let totalSize = 0;
    state.videos.forEach((video, index) => {
      totalDuration += Number(video.duration) || 0;
      totalSize += Number(video.size_bytes) || 0;
      const fragment = elements.videoCardTemplate.content.cloneNode(true);
      const cardButton = fragment.querySelector(".video-card-open");
      const image = fragment.querySelector(".video-thumb");
      image.src = `/api/videos/${encodeURIComponent(video.id)}/thumbnail?v=${Math.floor(video.modified_at || 0)}`;
      image.alt = `Preview of ${video.filename}`;
      image.onerror = () => { image.style.opacity = "0"; };
      fragment.querySelector(".duration-pill").textContent = formatDuration(video.duration);
      fragment.querySelector("h3").textContent = video.filename;
      fragment.querySelector(".card-path").textContent = video.relative_path;
      const meta = fragment.querySelector(".video-meta");
      const dimensions = video.width && video.height ? `${video.width}×${video.height}` : "Unknown size";
      [dimensions, video.video_codec || "unknown codec", formatBytes(video.size_bytes)].forEach((value) => {
        const detail = document.createElement("span");
        detail.textContent = value;
        meta.append(detail);
      });
      const job = queuedVideoIds.get(video.id);
      if (job) {
        const status = fragment.querySelector(".status-pill");
        status.textContent = job.status;
        status.classList.remove("hidden");
        status.classList.add(job.status);
      }
      cardButton.addEventListener("click", () => openEditor(index));

      const deleteButton = fragment.querySelector(".delete-video");
      const isActive = Boolean(job && ["queued", "processing"].includes(job.status));
      deleteButton.disabled = isActive;
      if (isActive) {
        deleteButton.title = "This source cannot be deleted while it is queued or processing.";
        deleteButton.setAttribute("aria-label", deleteButton.title);
      }
      deleteButton.addEventListener("click", () => deleteVideo(video));

      elements.gallery.append(fragment);
    });

    elements.heroStats.innerHTML = "";
    const stats = [
      [state.videos.length, "videos shown"],
      [formatDuration(totalDuration), "total duration"],
      [formatBytes(totalSize), "source media"],
    ];
    stats.forEach(([value, label]) => {
      const stat = document.createElement("div");
      stat.className = "stat";
      stat.innerHTML = `<b>${value}</b><span>${label}</span>`;
      elements.heroStats.append(stat);
    });
  }

  function renderQueue() {
    const active = state.jobs.filter((job) => ["queued", "processing"].includes(job.status));
    elements.queueCount.textContent = active.length;
    elements.queueDot.classList.toggle("active", active.length > 0);
    elements.clearQueueButton.disabled = state.jobs.length === 0;
    elements.queueJobs.innerHTML = "";
    if (!state.jobs.length) {
      const blank = document.createElement("p");
      blank.className = "job-message";
      blank.textContent = "No queued or completed jobs yet.";
      elements.queueJobs.append(blank);
      return;
    }

    state.jobs.forEach((job) => {
      const fragment = elements.queueJobTemplate.content.cloneNode(true);
      const status = fragment.querySelector(".job-status");
      status.textContent = job.status;
      status.classList.add(job.status);
      fragment.querySelector("h3").textContent = job.source_filename;
      fragment.querySelector(".job-cut").textContent = `Remove ${formatTime(job.cut_start)} → ${formatTime(job.cut_end)} (${job.cut_duration.toFixed(1)} sec)`;
      const progress = Math.max(0, Math.min(100, Number(job.progress) || 0));
      fragment.querySelector(".job-progress span").style.width = `${progress}%`;
      const message = fragment.querySelector(".job-message");
      if (job.status === "complete") {
        message.textContent = completeJobMessage(job);
        if (job.source_cleanup_error) message.classList.add("error");
      } else if (job.status === "failed") {
        message.textContent = job.error || "Processing failed.";
        message.classList.add("error");
      } else if (job.status === "cancelled") {
        message.textContent = "Cancelled. The source video remains unchanged.";
      } else if (job.status === "processing") {
        message.textContent = `${progress.toFixed(0)}% processed${job.delete_source_on_success ? " • original will be deleted after a successful save" : ""}`;
      } else {
        message.textContent = job.delete_source_on_success ? "Waiting for the worker. Original will be deleted after a successful save." : "Waiting for the worker.";
      }
      if (["queued", "processing"].includes(job.status)) {
        const cancel = fragment.querySelector(".cancel-job");
        cancel.classList.remove("hidden");
        cancel.addEventListener("click", async () => {
          try { await api(`/api/jobs/${job.id}/cancel`, { method: "POST" }); await refreshAll(); showToast("Job cancelled."); }
          catch (error) { showToast(error.message, "error"); }
        });
      }
      elements.queueJobs.append(fragment);
    });
  }

  function completeJobMessage(job) {
    let message = `Saved: ${job.output_relative || "output video"}`;
    const outputSize = Number(job.output_size_bytes) || 0;
    const sourceSize = Number(job.source_size_bytes) || 0;
    if (outputSize) {
      message += ` • ${formatBytes(outputSize)}`;
      if (sourceSize) message += ` (${Math.round((outputSize / sourceSize) * 100)}% of source)`;
    }
    if (job.size_limited) message += " • size guard applied";
    if (job.source_removed_at) message += " • original deleted";
    else if (job.source_cleanup_error) message += ` • output saved, but original was kept: ${job.source_cleanup_error}`;
    else if (job.delete_source_on_success) message += " • original removal was requested";
    if (job.size_guard_note) message += ` • ${job.size_guard_note}`;
    return message;
  }

  function openEditor(index) {
    if (index < 0 || index >= state.videos.length) return;
    state.currentIndex = index;
    state.currentVideo = state.videos[index];
    const video = state.currentVideo;
    state.duration = Math.max(0.1, Number(video.duration) || 0.1);
    const start = 0;
    const defaultCutEnd = 5.0;
    const end = Math.min(state.duration - 0.05, defaultCutEnd);

    elements.editorPosition.textContent = `VIDEO ${index + 1} OF ${state.videos.length}`;
    elements.editorTitle.textContent = video.filename;
    elements.editorPath.textContent = video.relative_path;
    elements.durationLabel.textContent = formatDuration(state.duration);
    [elements.cutStartRange, elements.cutEndRange].forEach((range) => { range.max = String(state.duration); });
    [elements.cutStartInput, elements.cutEndInput].forEach((input) => { input.max = String(state.duration); });
    elements.deleteSourceOnSuccess.checked = true;
    setCutValues(start, end);
    elements.editorVideo.pause();
    elements.editorVideo.src = `/api/videos/${encodeURIComponent(video.id)}/stream`;
    elements.editorVideo.load();
    elements.previewUnavailable.classList.add("hidden");
    elements.editorModal.classList.remove("hidden");
    document.body.style.overflow = "hidden";
    elements.previousButton.disabled = index <= 0;
  }

  function closeEditor() {
    state.previewingCut = false;
    elements.editorVideo.pause();
    elements.editorVideo.removeAttribute("src");
    elements.editorVideo.load();
    elements.editorModal.classList.add("hidden");
    document.body.style.overflow = "";
  }

  function clampCutValues(start, end) {
    const epsilon = Math.min(0.1, state.duration / 100);
    let safeStart = Math.max(0, Math.min(Number(start) || 0, state.duration - epsilon));
    let safeEnd = Math.max(epsilon, Math.min(Number(end) || epsilon, state.duration));
    if (safeEnd <= safeStart + epsilon) {
      if (safeStart + epsilon <= state.duration) safeEnd = safeStart + epsilon;
      else safeStart = Math.max(0, safeEnd - epsilon);
    }
    return [safeStart, safeEnd];
  }

  function setCutValues(start, end) {
    const [safeStart, safeEnd] = clampCutValues(start, end);
    elements.cutStartRange.value = String(safeStart);
    elements.cutEndRange.value = String(safeEnd);
    elements.cutStartInput.value = safeStart.toFixed(1);
    elements.cutEndInput.value = safeEnd.toFixed(1);
    updateCutDisplay();
  }

  function getCutValues() {
    return clampCutValues(elements.cutStartRange.value, elements.cutEndRange.value);
  }

  function updateCutDisplay() {
    const [start, end] = getCutValues();
    const leftPct = (start / state.duration) * 100;
    const cutPct = ((end - start) / state.duration) * 100;
    elements.timelineCut.style.marginLeft = `${leftPct}%`;
    elements.timelineCut.style.width = `${cutPct}%`;
    elements.removeSummary.textContent = `${formatTime(start)} → ${formatTime(end)}`;
    elements.removedLength.textContent = `${(end - start).toFixed(1)} sec`;
  }

  function moveEditor(delta) {
    const next = state.currentIndex + delta;
    if (next >= 0 && next < state.videos.length) openEditor(next);
  }

  async function skipCurrentVideo() {
    if (!state.currentVideo) return;
    const video = state.currentVideo;
    try {
      await api(`/api/videos/${video.id}/skip`, { method: "POST", body: JSON.stringify({ skipped: true }) });
      const nextIndex = Math.min(state.currentIndex, state.videos.length - 2);
      state.videos.splice(state.currentIndex, 1);
      renderGallery();
      if (state.videos.length) openEditor(Math.max(0, nextIndex));
      else closeEditor();
      showToast(`Skipped ${video.filename}. It can be restored by enabling Show skipped.`);
    } catch (error) { showToast(error.message, "error"); }
  }

  async function clearQueue() {
    if (!state.jobs.length) return;

    const activeJobs = state.jobs.filter((job) => ["queued", "processing"].includes(job.status));
    const message = activeJobs.length
      ? `Clear all ${state.jobs.length} queue item${state.jobs.length === 1 ? "" : "s"}? ${activeJobs.length} active job${activeJobs.length === 1 ? "" : "s"} will be cancelled. Source and finished output files will be kept.`
      : `Clear all ${state.jobs.length} queue item${state.jobs.length === 1 ? "" : "s"}? Source and finished output files will be kept.`;

    if (!window.confirm(message)) return;

    elements.clearQueueButton.disabled = true;
    try {
      const result = await api("/api/jobs/clear", { method: "POST" });
      await refreshAll();
      const processingMessage = result.cancelled_processing
        ? ` Cancelled ${result.cancelled_processing} processing job${result.cancelled_processing === 1 ? "" : "s"}.`
        : "";
      showToast(`Cleared ${result.cleared} queue item${result.cleared === 1 ? "" : "s"}.${processingMessage} Media files were kept.`, "success");
    } catch (error) {
      showToast(error.message, "error");
      await refreshAll({ quiet: true });
    }
  }

  async function deleteVideo(video) {
    const confirmed = window.confirm(`Delete “${video.filename}” from the input folder?\n\nThis permanently removes the source file. Output files and completed job history are kept.`);
    if (!confirmed) return;
    try {
      await api(`/api/videos/${encodeURIComponent(video.id)}`, { method: "DELETE" });
      if (state.currentVideo && state.currentVideo.id === video.id) closeEditor();
      await refreshAll({ quiet: true });
      showToast(`${video.filename} was deleted from the input folder.`, "success");
    } catch (error) {
      showToast(error.message, "error");
    }
  }

  async function queueCurrentCut() {
    if (!state.currentVideo) return;
    const [cutStart, cutEnd] = getCutValues();
    const deleteSourceOnSuccess = elements.deleteSourceOnSuccess.checked;
    elements.queueCutButton.disabled = true;
    elements.queueCutButton.textContent = "Adding to queue…";
    try {
      await api("/api/jobs", {
        method: "POST",
        body: JSON.stringify({
          video_id: state.currentVideo.id,
          cut_start: cutStart,
          cut_end: cutEnd,
          delete_source_on_success: deleteSourceOnSuccess,
        }),
      });
      const name = state.currentVideo.filename;
      const nextIndex = Math.min(state.currentIndex, state.videos.length - 2);
      state.videos.splice(state.currentIndex, 1);
      renderGallery();
      await refreshAll({ quiet: true });
      if (state.videos.length) openEditor(Math.max(0, nextIndex));
      else closeEditor();
      const suffix = deleteSourceOnSuccess ? " The original will be removed only after the output is safely saved." : "";
      showToast(`${name} was added to the processing queue.${suffix}`, "success");
    } catch (error) {
      showToast(error.message, "error");
    } finally {
      elements.queueCutButton.disabled = false;
      elements.queueCutButton.innerHTML = 'Queue cut &amp; next <span>→</span>';
    }
  }

  function openQueue() {
    elements.queuePanel.classList.add("open");
    elements.queuePanel.setAttribute("aria-hidden", "false");
    elements.queueBackdrop.classList.remove("hidden");
  }

  function closeQueue() {
    elements.queuePanel.classList.remove("open");
    elements.queuePanel.setAttribute("aria-hidden", "true");
    elements.queueBackdrop.classList.add("hidden");
  }

  function wireEvents() {
    elements.rescanButton.addEventListener("click", async () => {
      elements.rescanButton.disabled = true;
      elements.rescanButton.innerHTML = "Scanning…";
      try {
        const result = await api("/api/videos/rescan", { method: "POST" });
        await refreshAll({ quiet: true });
        const ignored = Array.isArray(result.ignored) ? result.ignored : [];
        if (ignored.length) {
          const first = ignored[0];
          showToast(`Scan found ${result.total} file${result.total === 1 ? "" : "s"}, but could not index ${first.path}: ${first.error}`, "error");
        } else {
          showToast(`Scan complete: ${result.total} video${result.total === 1 ? "" : "s"} found.`, "success");
        }
      }
      catch (error) { showToast(error.message, "error"); }
      finally { elements.rescanButton.disabled = false; elements.rescanButton.innerHTML = "<span>↻</span> Rescan folder"; }
    });
    elements.showSkipped.addEventListener("change", () => refreshAll());
    elements.queueButton.addEventListener("click", openQueue);
    elements.closeQueueButton.addEventListener("click", closeQueue);
    elements.clearQueueButton.addEventListener("click", clearQueue);
    elements.queueBackdrop.addEventListener("click", closeQueue);
    elements.closeEditorButton.addEventListener("click", closeEditor);
    elements.editorModal.querySelector(".modal-backdrop").addEventListener("click", closeEditor);
    elements.previousButton.addEventListener("click", () => moveEditor(-1));
    elements.skipButton.addEventListener("click", skipCurrentVideo);
    elements.queueCutButton.addEventListener("click", queueCurrentCut);
    elements.setStartButton.addEventListener("click", () => setCutValues(elements.editorVideo.currentTime || 0, getCutValues()[1]));
    elements.setEndButton.addEventListener("click", () => setCutValues(getCutValues()[0], elements.editorVideo.currentTime || 0));
    elements.previewCutButton.addEventListener("click", () => {
      const [start] = getCutValues();
      state.previewingCut = true;
      elements.editorVideo.currentTime = start;
      elements.editorVideo.play().catch(() => showToast("Browser preview could not start for this video.", "error"));
    });

    elements.cutStartRange.addEventListener("input", () => setCutValues(elements.cutStartRange.value, getCutValues()[1]));
    elements.cutEndRange.addEventListener("input", () => setCutValues(getCutValues()[0], elements.cutEndRange.value));
    elements.cutStartInput.addEventListener("change", () => setCutValues(elements.cutStartInput.value, getCutValues()[1]));
    elements.cutEndInput.addEventListener("change", () => setCutValues(getCutValues()[0], elements.cutEndInput.value));
    elements.editorVideo.addEventListener("timeupdate", () => {
      elements.playheadReadout.textContent = formatTime(elements.editorVideo.currentTime);
      if (state.previewingCut && elements.editorVideo.currentTime >= getCutValues()[1]) {
        state.previewingCut = false;
        elements.editorVideo.pause();
      }
    });
    elements.editorVideo.addEventListener("error", () => elements.previewUnavailable.classList.remove("hidden"));
    elements.editorVideo.addEventListener("loadedmetadata", () => {
      if (Number.isFinite(elements.editorVideo.duration) && elements.editorVideo.duration > 0) elements.previewUnavailable.classList.add("hidden");
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { closeEditor(); closeQueue(); }
      if (elements.editorModal.classList.contains("hidden")) return;
      if (event.key === "ArrowRight" && !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) moveEditor(1);
      if (event.key === "ArrowLeft" && !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) moveEditor(-1);
    });
  }

  async function initialise() {
    wireEvents();
    await refreshAll();
    state.refreshTimer = window.setInterval(() => refreshAll({ quiet: true }), 3500);
  }

  initialise();
})();
