/**
 * OmniStream — Interactive Frontend Logic & Queue Management
 */

// Application State
let currentTab = 'single';
let currentFilter = 'all';
let allTasks = [];
let pollInterval = null;

// User ID Management
function getUserId() {
  let uid = localStorage.getItem('omni_user_id');
  if (!uid) {
    uid = 'user_' + Math.random().toString(36).substring(2, 10);
    localStorage.setItem('omni_user_id', uid);
  }
  return uid;
}

// Initialize on DOM Ready
document.addEventListener('DOMContentLoaded', () => {
  const urlParams = new URLSearchParams(window.location.search);
  if (urlParams.get('auth_status') === 'success') {
    showToast('YouTube channel connected successfully!', 'success');
    window.history.replaceState({}, document.title, window.location.pathname);
  }

  fetchQueue();
  fetchStats();
  checkYouTubeStatus();
  checkTelegramStatus();
  startPolling();
});

// --- YouTube Status & Options ---
async function checkYouTubeStatus() {
  try {
    const uid = getUserId();
    const res = await fetch(`/api/youtube/status?user_id=${encodeURIComponent(uid)}`);
    if (!res.ok) return;
    const data = await res.json();
    const singleBadge = document.getElementById('ytAuthBadgeSingle');
    const batchBadge = document.getElementById('ytAuthBadgeBatch');
    const headerBtn = document.getElementById('btnOpenYtModal');
    const headerText = document.getElementById('headerYtStatusText');

    if (data.authenticated) {
      if (singleBadge) { singleBadge.textContent = '● Authenticated'; singleBadge.className = 'yt-status-tag active'; }
      if (batchBadge) { batchBadge.textContent = '● Authenticated'; batchBadge.className = 'yt-status-tag active'; }
      if (headerBtn) headerBtn.classList.add('connected');
      if (headerText) headerText.textContent = data.channel_name || 'YouTube Connected';
      updateModalStatus(true, data.channel_name);
    } else {
      if (singleBadge) { singleBadge.textContent = '⚠ Auth Needed'; singleBadge.className = 'yt-status-tag warning'; }
      if (batchBadge) { batchBadge.textContent = '⚠ Auth Needed'; batchBadge.className = 'yt-status-tag warning'; }
      if (headerBtn) headerBtn.classList.remove('connected');
      if (headerText) headerText.textContent = 'Link YouTube';
      updateModalStatus(false);
    }
  } catch (e) {
    console.log('YouTube status check error:', e);
  }
}

function updateModalStatus(connected, channelName) {
  const dot = document.getElementById('modalYtDot');
  const msg = document.getElementById('modalYtStatusMsg');
  const btn = document.getElementById('btnGoogleConnect');
  if (connected) {
    if (dot) { dot.className = 'status-indicator-dot active'; }
    if (msg) { msg.textContent = `Connected: ${channelName || 'Your YouTube Channel'}`; }
    if (btn) { btn.textContent = 'Channel Already Linked (Click to Re-link)'; btn.disabled = false; }
  } else {
    if (dot) { dot.className = 'status-indicator-dot'; }
    if (msg) { msg.textContent = 'Not connected. Please upload client_secrets.json first.'; }
  }
}

function openYtModal() {
  const overlay = document.getElementById('ytModalOverlay');
  const uidVal = document.getElementById('modalUserId');
  if (uidVal) uidVal.textContent = getUserId();
  if (overlay) overlay.classList.remove('hidden');
}

function closeYtModal(e) {
  const overlay = document.getElementById('ytModalOverlay');
  if (overlay) overlay.classList.add('hidden');
}

async function handleSecretsFile(file) {
  if (!file) return;
  const statusDiv = document.getElementById('fileUploadStatus');
  const btnConnect = document.getElementById('btnGoogleConnect');
  statusDiv.textContent = 'Reading file...';
  statusDiv.style.color = 'var(--text-muted)';

  try {
    const text = await file.text();
    const json = JSON.parse(text);
    const uid = getUserId();

    statusDiv.textContent = 'Saving credentials to Cloudflare...';
    const res = await fetch('/api/user/save-secrets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: uid, client_secrets: json })
    });

    const data = await res.json();
    if (res.ok && data.success) {
      statusDiv.textContent = '✓ client_secrets.json saved! Now click Step 2.';
      statusDiv.style.color = 'var(--accent-emerald)';
      if (btnConnect) btnConnect.disabled = false;
      showToast('Credentials saved successfully!', 'success');
    } else {
      statusDiv.textContent = `✕ Error: ${data.detail || data.error || 'Failed to save'}`;
      statusDiv.style.color = 'var(--accent-rose)';
    }
  } catch (err) {
    statusDiv.textContent = '✕ Invalid JSON file. Please upload a valid client_secrets.json';
    statusDiv.style.color = 'var(--accent-rose)';
  }
}

async function startGoogleAuth() {
  const uid = getUserId();
  try {
    const res = await fetch(`/api/user/login-url?user_id=${encodeURIComponent(uid)}`);
    const data = await res.json();
    if (data.url) {
      window.location.href = data.url;
    } else {
      showToast(data.error || 'Authentication URL not available.', 'error');
    }
  } catch (err) {
    showToast('Failed to start Google sign-in.', 'error');
  }
}

function toggleYtPrivacy(mode) {
  const checkbox = document.getElementById(`${mode}YtUpload`);
  const options = document.getElementById(`${mode}YtOptions`);
  if (checkbox && options) {
    options.classList.toggle('hidden', !checkbox.checked);
  }
}

// --- Telegram Status & Settings Management ---
function getTelegramConfig() {
  return {
    bot_token: localStorage.getItem('omni_tg_bot_token') || '',
    api_id: localStorage.getItem('omni_tg_api_id') || '',
    api_hash: localStorage.getItem('omni_tg_api_hash') || '',
    default_chat: localStorage.getItem('omni_tg_default_chat') || '',
    bot_username: localStorage.getItem('omni_tg_bot_username') || ''
  };
}

function checkTelegramStatus() {
  const cfg = getTelegramConfig();
  const headerBtn = document.getElementById('btnOpenTgModal');
  const headerText = document.getElementById('headerTgStatusText');
  const singleBadge = document.getElementById('tgConfigBadgeSingle');
  const batchBadge = document.getElementById('tgConfigBadgeBatch');
  const modalDot = document.getElementById('modalTgDot');
  const modalMsg = document.getElementById('modalTgStatusMsg');

  const isConfigured = Boolean(cfg.bot_token && cfg.api_id && cfg.api_hash);

  if (isConfigured) {
    const displayBot = cfg.bot_username ? `@${cfg.bot_username}` : 'TG Bot Ready';
    if (headerBtn) headerBtn.classList.add('connected');
    if (headerText) headerText.textContent = displayBot;
    if (singleBadge) { singleBadge.textContent = '● ' + displayBot; singleBadge.className = 'tg-status-tag active'; }
    if (batchBadge) { batchBadge.textContent = '● ' + displayBot; batchBadge.className = 'tg-status-tag active'; }
    if (modalDot) modalDot.className = 'status-indicator-dot active';
    if (modalMsg) modalMsg.textContent = `Connected as ${displayBot}`;
  } else {
    if (headerBtn) headerBtn.classList.remove('connected');
    if (headerText) headerText.textContent = 'Link Telegram';
    if (singleBadge) { singleBadge.textContent = 'Config Needed'; singleBadge.className = 'tg-status-tag'; }
    if (batchBadge) { batchBadge.textContent = 'Config Needed'; batchBadge.className = 'tg-status-tag'; }
    if (modalDot) modalDot.className = 'status-indicator-dot';
    if (modalMsg) modalMsg.textContent = 'No Bot Token saved yet';
  }
}

function openTgModal() {
  const overlay = document.getElementById('tgModalOverlay');
  const cfg = getTelegramConfig();
  const tokenInp = document.getElementById('tgModalBotToken');
  const apiIdInp = document.getElementById('tgModalApiId');
  const apiHashInp = document.getElementById('tgModalApiHash');
  const chatInp = document.getElementById('tgModalDefaultChat');

  if (tokenInp) tokenInp.value = cfg.bot_token;
  if (apiIdInp) apiIdInp.value = cfg.api_id;
  if (apiHashInp) apiHashInp.value = cfg.api_hash;
  if (chatInp) chatInp.value = cfg.default_chat;

  checkTelegramStatus();
  if (overlay) overlay.classList.remove('hidden');
}

function closeTgModal(e) {
  const overlay = document.getElementById('tgModalOverlay');
  if (overlay) overlay.classList.add('hidden');
}

function toggleTgOptions(mode) {
  const checkbox = document.getElementById(`${mode}TgUpload`);
  const options = document.getElementById(`${mode}TgOptions`);
  const chatInput = document.getElementById(`${mode}TgChatId`);
  if (checkbox && options) {
    options.classList.toggle('hidden', !checkbox.checked);
    if (checkbox.checked && chatInput && !chatInput.value) {
      const cfg = getTelegramConfig();
      if (cfg.default_chat) chatInput.value = cfg.default_chat;
    }
  }
}

async function saveTelegramSettings(e) {
  e.preventDefault();
  const botToken = document.getElementById('tgModalBotToken').value.trim();
  const apiId = document.getElementById('tgModalApiId').value.trim();
  const apiHash = document.getElementById('tgModalApiHash').value.trim();
  const defaultChat = document.getElementById('tgModalDefaultChat').value.trim();
  const btn = document.getElementById('btnSaveTg');

  if (!botToken || !apiId || !apiHash) {
    showToast('Please fill in Bot Token, API ID, and API Hash', 'error');
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Testing Token...';

  try {
    const res = await fetch('/api/telegram/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bot_token: botToken })
    });
    const data = await res.json();
    if (data.valid) {
      localStorage.setItem('omni_tg_bot_token', botToken);
      localStorage.setItem('omni_tg_api_id', apiId);
      localStorage.setItem('omni_tg_api_hash', apiHash);
      if (defaultChat) localStorage.setItem('omni_tg_default_chat', defaultChat);
      if (data.username) localStorage.setItem('omni_tg_bot_username', data.username);

      checkTelegramStatus();
      if (data.pyrogram_available === false) {
        showToast(`Bot @${data.username || 'Bot'} verified, BUT Pyrofork is missing on server! Run: !pip install pyrofork tgcrypto in Colab.`, 'warning');
      } else {
        showToast(`Telegram Bot Verified! Connected: @${data.username || data.first_name}`, 'success');
      }
      closeTgModal();
    } else {
      showToast(`Telegram Error: ${data.error || 'Invalid Bot Token'}`, 'error');
    }
  } catch (err) {
    showToast('Network error verifying Telegram bot credentials', 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = `
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
        <polyline points="20 6 9 17 4 12"></polyline>
      </svg>
      Test & Save Credentials
    `;
  }
}


// --- Tab Switching ---
function switchTab(tab) {
  currentTab = tab;
  const singleBtn = document.getElementById('tabSingleBtn');
  const batchBtn = document.getElementById('tabBatchBtn');
  const singleForm = document.getElementById('singleForm');
  const batchForm = document.getElementById('batchForm');

  if (tab === 'single') {
    singleBtn.classList.add('active');
    batchBtn.classList.remove('active');
    singleForm.classList.remove('hidden');
    batchForm.classList.add('hidden');
  } else {
    batchBtn.classList.add('active');
    singleBtn.classList.remove('active');
    batchForm.classList.remove('hidden');
    singleForm.classList.add('hidden');
  }
}

// --- Clipboard Helper ---
async function pasteClipboard(targetInputId) {
  try {
    const text = await navigator.clipboard.readText();
    if (text) {
      const input = document.getElementById(targetInputId);
      input.value = text.trim();
      input.focus();
      showToast('URL pasted from clipboard', 'info');
      if (targetInputId === 'batchText') updateBatchCount();
    }
  } catch (err) {
    showToast('Clipboard access denied. Please paste manually.', 'error');
  }
}

// --- Batch URL Count Parser ---
function updateBatchCount() {
  const text = document.getElementById('batchText').value;
  const lines = text.split('\n').filter(l => l.trim().length > 0 && l.includes('http'));
  const counter = document.getElementById('batchLinkCount');
  counter.textContent = `${lines.length} link${lines.length === 1 ? '' : 's'} detected`;
}

// --- Submit Single Download ---
async function handleSingleSubmit(e) {
  e.preventDefault();
  const urlInput = document.getElementById('singleUrl');
  const nameInput = document.getElementById('singleName');
  const resInput = document.querySelector('input[name="singleRes"]:checked');
  const ytUpload = document.getElementById('singleYtUpload')?.checked || false;
  const ytPrivacy = document.getElementById('singleYtPrivacy')?.value || 'unlisted';
  const tgUpload = document.getElementById('singleTgUpload')?.checked || false;
  const tgChatId = document.getElementById('singleTgChatId')?.value.trim() || '';
  const tgConfig = getTelegramConfig();

  if (tgUpload && (!tgConfig.bot_token || !tgConfig.api_id || !tgConfig.api_hash)) {
    showToast('Please configure Telegram Bot Token & API keys first!', 'error');
    openTgModal();
    return;
  }

  const url = urlInput.value.trim();
  const customName = nameInput.value.trim();
  const resolution = resInput ? resInput.value : '720';

  if (!url) {
    showToast('Please enter a valid video URL', 'error');
    return;
  }

  const submitBtn = document.getElementById('btnSingleSubmit');
  submitBtn.disabled = true;

  try {
    const res = await fetch('/api/download/single', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        url: url,
        custom_name: customName || null,
        resolution: resolution,
        upload_to_youtube: ytUpload,
        privacy_status: ytPrivacy,
        user_id: getUserId(),
        upload_to_telegram: tgUpload,
        tg_bot_token: tgConfig.bot_token || null,
        tg_api_id: tgConfig.api_id || null,
        tg_api_hash: tgConfig.api_hash || null,
        tg_chat_id: tgChatId || tgConfig.default_chat || null
      })
    });

    const data = await res.json();
    if (res.ok && data.success) {
      showToast(ytUpload ? 'Video queued + YouTube upload enabled!' : 'Video added to download queue!', 'success');
      urlInput.value = '';
      nameInput.value = '';
      await fetchQueue();
      await fetchStats();
    } else {
      showToast(data.detail || 'Failed to add video.', 'error');
    }
  } catch (err) {
    showToast('Network error while adding video.', 'error');
  } finally {
    submitBtn.disabled = false;
  }
}

// --- Submit Batch Download ---
async function handleBatchSubmit(e) {
  e.preventDefault();
  const textArea = document.getElementById('batchText');
  const resInput = document.querySelector('input[name="batchRes"]:checked');
  const ytUpload = document.getElementById('batchYtUpload')?.checked || false;
  const ytPrivacy = document.getElementById('batchYtPrivacy')?.value || 'unlisted';
  const tgUpload = document.getElementById('batchTgUpload')?.checked || false;
  const tgChatId = document.getElementById('batchTgChatId')?.value.trim() || '';
  const tgConfig = getTelegramConfig();

  if (tgUpload && (!tgConfig.bot_token || !tgConfig.api_id || !tgConfig.api_hash)) {
    showToast('Please configure Telegram Bot Token & API keys first!', 'error');
    openTgModal();
    return;
  }

  const rawText = textArea.value.trim();
  const defaultRes = resInput ? resInput.value : '720';

  if (!rawText) {
    showToast('Please paste at least one video link.', 'error');
    return;
  }

  const submitBtn = document.getElementById('btnBatchSubmit');
  submitBtn.disabled = true;

  try {
    const res = await fetch('/api/download/batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        raw_text: rawText,
        default_resolution: defaultRes,
        upload_to_youtube: ytUpload,
        default_privacy: ytPrivacy,
        user_id: getUserId(),
        upload_to_telegram: tgUpload,
        tg_bot_token: tgConfig.bot_token || null,
        tg_api_id: tgConfig.api_id || null,
        tg_api_hash: tgConfig.api_hash || null,
        tg_chat_id: tgChatId || tgConfig.default_chat || null
      })
    });

    const data = await res.json();
    if (res.ok && data.success) {
      showToast(`Enqueued ${data.count} videos successfully!`, 'success');
      textArea.value = '';
      updateBatchCount();
      await fetchQueue();
      await fetchStats();
    } else {
      showToast(data.detail || 'Failed to process batch.', 'error');
    }
  } catch (err) {
    showToast('Network error while enqueueing batch.', 'error');
  } finally {
    submitBtn.disabled = false;
  }
}


// --- Polling & Data Fetching (Refreshes every 2 seconds) ---
function startPolling() {
  if (pollInterval) clearInterval(pollInterval);
  pollInterval = setInterval(async () => {
    await fetchQueue();
    await fetchStats();
  }, 2000);
}

// Automatically resume & refresh whenever user switches back to this tab
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) {
    fetchQueue();
    fetchStats();
    startPolling();
  }
});

// Manual Refresh for Queue & Progress
async function manualRefreshQueue() {
  const btn = document.getElementById('btnQueueRefresh');
  if (btn) btn.classList.add('spinning');
  try {
    await fetchQueue();
    await fetchStats();
    showToast('Download queue refreshed!', 'info');
  } catch (err) {
    showToast('Failed to refresh queue', 'error');
  } finally {
    setTimeout(() => {
      if (btn) btn.classList.remove('spinning');
    }, 600);
  }
}

async function fetchQueue() {
  try {
    const uid = getUserId();
    const res = await fetch(`/api/queue?user_id=${encodeURIComponent(uid)}`);
    if (!res.ok) return;
    const data = await res.json();
    allTasks = data.tasks || [];
    renderQueue();
  } catch (err) {
    console.error('Failed to fetch queue:', err);
  }
}

async function fetchStats() {
  try {
    const uid = getUserId();
    const res = await fetch(`/api/stats?user_id=${encodeURIComponent(uid)}`);
    if (!res.ok) return;
    const stats = await res.json();
    document.getElementById('statActive').textContent = stats.active_tasks;
    document.getElementById('statQueue').textContent = stats.queue_length;
    document.getElementById('statCompleted').textContent = stats.completed_tasks;
    document.getElementById('statDisk').textContent = stats.disk_usage;
    document.getElementById('queueTotalBadge').textContent = stats.total_tasks;
  } catch (err) {
    console.error('Failed to fetch stats:', err);
  }
}

// --- Queue Filter ---
function setFilter(filter) {
  currentFilter = filter;
  document.querySelectorAll('.filter-pill').forEach(btn => {
    btn.classList.toggle('active', btn.textContent.toLowerCase().includes(filter));
  });
  renderQueue();
}

// --- Delete Single Task ---
async function deleteTask(taskId) {
  try {
    const uid = getUserId();
    const res = await fetch(`/api/queue/${taskId}?user_id=${encodeURIComponent(uid)}`, { method: 'DELETE' });
    if (res.ok) {
      showToast('Task removed from queue', 'info');
      await fetchQueue();
      await fetchStats();
    }
  } catch (err) {
    showToast('Failed to delete task', 'error');
  }
}

// --- Clear Finished Queue ---
async function clearFinishedQueue() {
  try {
    const uid = getUserId();
    const res = await fetch(`/api/queue/clear?user_id=${encodeURIComponent(uid)}`, { method: 'POST' });
    if (res.ok) {
      const data = await res.json();
      showToast(`Cleared ${data.cleared_count} finished tasks`, 'info');
      await fetchQueue();
      await fetchStats();
    }
  } catch (err) {
    showToast('Failed to clear queue', 'error');
  }
}

// --- Render Queue Cards ---
function renderQueue() {
  const container = document.getElementById('queueList');
  const emptyState = document.getElementById('emptyState');

  // Filter tasks
  let filtered = allTasks;
  if (currentFilter === 'active') {
    filtered = allTasks.filter(t => ['queued', 'downloading', 'processing'].includes(t.status));
  } else if (currentFilter === 'completed') {
    filtered = allTasks.filter(t => t.status === 'completed');
  } else if (currentFilter === 'failed') {
    filtered = allTasks.filter(t => t.status === 'failed');
  }

  if (filtered.length === 0) {
    container.innerHTML = '';
    container.appendChild(emptyState);
    emptyState.style.display = 'flex';
    return;
  }

  emptyState.style.display = 'none';

  // In-place DOM update to eliminate re-rendering flicker and jump
  const existingCardIds = Array.from(container.querySelectorAll('.task-card')).map(c => c.id.replace('card-', ''));
  const filteredIds = filtered.map(t => t.id);
  const canUpdateInPlace = existingCardIds.length === filteredIds.length && 
                           existingCardIds.every((id, idx) => id === filteredIds[idx]);

  if (canUpdateInPlace) {
    let requiresFullRebuild = false;
    filtered.forEach(task => {
      const card = document.getElementById(`card-${task.id}`);
      if (!card) { requiresFullRebuild = true; return; }

      const progressPercent = task.progress || 0;
      const speed = task.speed_str || '--';
      const eta = task.eta_str || '--';
      const filesize = task.filesize_str || '--';
      const isDownloading = task.status === 'downloading';
      const isUploading = task.status === 'uploading';
      const isCompleted = task.status === 'completed';

      // Check if status changed
      if (!card.classList.contains(`status-${task.status}`)) {
        requiresFullRebuild = true;
        return;
      }

      // Smooth progress bar slide
      const fill = card.querySelector('.progress-fill');
      if (fill) {
        fill.style.width = `${progressPercent}%`;
        fill.classList.toggle('done', isCompleted);
      }

      // Stats: % and speed
      const statPercent = card.querySelector('.task-stats .stat-item:first-child');
      if (statPercent) {
        statPercent.innerHTML = `<span>${progressPercent}%</span>${(isDownloading || isUploading) ? `<span>• ${speed}</span>` : ''}`;
      }

      // Stats: ETA or error or YouTube status
      const statEta = card.querySelector('.task-stats .stat-item:last-child');
      if (statEta) {
        if (isDownloading) {
          statEta.innerHTML = `<span>ETA: ${eta}</span>`;
        } else if (task.status === 'failed') {
          statEta.innerHTML = `<span style="color: var(--accent-rose);">${escapeHtml(task.error_message || 'Download Error')}</span>`;
        } else if (task.youtube_status && !task.youtube_url) {
          statEta.innerHTML = `<span style="color: #fbbf24;">${escapeHtml(task.youtube_status)}</span>`;
        } else if (task.telegram_status && !task.telegram_url) {
          statEta.innerHTML = `<span style="color: #38bdf8;">${escapeHtml(task.telegram_status)}</span>`;
        } else {
          statEta.innerHTML = '';
        }
      }

      // Filesize
      const sizeEl = card.querySelector('.task-meta span:last-child');
      if (sizeEl) {
        sizeEl.textContent = `Size: ${filesize}`;
      }
    });

    if (!requiresFullRebuild) {
      return;
    }
  }

  // Build Cards
  const cardsHtml = filtered.map(task => {
    const isDownloading = task.status === 'downloading';
    const isUploading = task.status === 'uploading';
    const isCompleted = task.status === 'completed';
    const isFailed = task.status === 'failed';
    const isProcessing = task.status === 'processing';
    const isQueued = task.status === 'queued';

    let statusBadgeClass = 'badge-queued';
    let statusText = 'Queued';

    if (isDownloading) {
      statusBadgeClass = 'badge-downloading';
      statusText = '<span class="pill-dot blue pulse"></span> Downloading';
    } else if (isUploading) {
      statusBadgeClass = 'badge-downloading';
      if (task.telegram_status === 'uploading') {
        statusText = '<span class="pill-dot blue pulse"></span> Uploading to Telegram...';
      } else {
        statusText = '<span class="pill-dot amber pulse"></span> Uploading to YouTube...';
      }
    } else if (isProcessing) {
      statusBadgeClass = 'badge-processing';
      statusText = 'Merging / Encoding';
    } else if (isCompleted) {
      statusBadgeClass = 'badge-completed';
      statusText = '✓ Completed';
    } else if (isFailed) {
      statusBadgeClass = 'badge-failed';
      statusText = '✕ Failed';
    }

    const progressPercent = task.progress || 0;
    const speed = task.speed_str || '--';
    const eta = task.eta_str || '--';
    const filesize = task.filesize_str || '--';

    return `
      <div class="task-card status-${escapeHtml(task.status)}" id="card-${task.id}">
        <div class="task-top">
          <div class="task-info">
            <h4 class="task-title" title="${escapeHtml(task.display_title)}">
              ${escapeHtml(task.display_title)}
            </h4>
            <div class="task-meta">
              <span class="badge-res">${escapeHtml(task.resolution.toUpperCase())}</span>
              ${task.media_type && task.media_type !== 'Standard' ? `<span class="badge-drm">⚡ ${escapeHtml(task.media_type)}</span>` : ''}
              ${task.upload_to_youtube ? `<span class="badge-drm" style="color:#ff6666; border-color:rgba(255,0,0,0.35); background:rgba(255,0,0,0.12);">🔴 YT (${escapeHtml(task.privacy_status || 'unlisted')})</span>` : ''}
              ${task.upload_to_telegram ? `<span class="badge-drm" style="color:#38bdf8; border-color:rgba(0,136,204,0.35); background:rgba(0,136,204,0.12);">✈ TG (${escapeHtml(task.tg_chat_id || 'Chat')})</span>` : ''}
              <span>Size: ${escapeHtml(filesize)}</span>
            </div>

          </div>
          <div class="status-badge ${statusBadgeClass}">
            ${statusText}
          </div>
        </div>

        <div class="task-progress-section">
          <div class="progress-track">
            <div class="progress-fill ${isCompleted ? 'done' : ''}" style="width: ${progressPercent}%;"></div>
          </div>
          <div class="task-stats">
            <div class="stat-item">
              <span>${progressPercent}%</span>
              ${isDownloading || isUploading ? `<span>• ${speed}</span>` : ''}
            </div>
            <div class="stat-item">
              ${isDownloading ? `<span>ETA: ${eta}</span>` : ''}
              ${isFailed ? `<span style="color: var(--accent-rose);">${escapeHtml(task.error_message || 'Download Error')}</span>` : ''}
              ${task.youtube_status && !task.youtube_url ? `<span style="color: #fbbf24;">${escapeHtml(task.youtube_status)}</span>` : ''}
              ${task.telegram_status && !task.telegram_url ? `<span style="color: #38bdf8;">${escapeHtml(task.telegram_status)}</span>` : ''}
            </div>
          </div>
        </div>

        <div class="task-bottom">
          <span class="task-url" title="${escapeHtml(task.url)}">${escapeHtml(task.url)}</span>
          <div class="task-actions">
            ${task.youtube_url ? `
              <a href="${escapeHtml(task.youtube_url)}" target="_blank" rel="noopener noreferrer" class="btn-yt-watch" title="Watch on YouTube">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                  <path d="M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 0 0 .502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 0 0 2.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814zM9.545 15.568V8.432L15.818 12l-6.273 3.568z"/>
                </svg>
                Watch on YouTube
              </a>
            ` : (isCompleted ? `
              <button class="btn-yt-watch" onclick="uploadTaskToYouTube('${task.id}')" title="Upload to YouTube">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                  <path d="M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 0 0 .502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 0 0 2.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814zM9.545 15.568V8.432L15.818 12l-6.273 3.568z"/>
                </svg>
                Upload to YT
              </button>
            ` : '')}
            ${task.telegram_url ? `
              <a href="${escapeHtml(task.telegram_url)}" target="_blank" rel="noopener noreferrer" class="btn-tg-watch" title="Watch on Telegram">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                  <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm4.64 6.8c-.15 1.58-.8 5.42-1.13 7.19-.14.75-.42 1-.68 1.03-.58.05-1.02-.38-1.58-.75-.88-.58-1.38-.94-2.23-1.5-.99-.65-.35-1.01.22-1.59.15-.15 2.71-2.48 2.76-2.69a.2.2 0 0 0-.05-.18c-.06-.05-.14-.03-.21-.02-.09.02-1.49.95-4.22 2.79-.4.27-.76.41-1.08.4-.36-.01-1.04-.2-1.55-.37-.63-.2-1.12-.31-1.08-.66.02-.18.27-.36.75-.55 2.92-1.27 4.86-2.11 5.83-2.51 2.78-1.16 3.35-1.36 3.73-1.36.08 0 .27.02.39.12.1.08.13.19.14.27-.01.06.01.24 0 .38z"/>
                </svg>
                Watch on TG
              </a>
            ` : (isCompleted ? `
              <button class="btn-tg-watch" onclick="uploadTaskToTelegram('${task.id}')" title="Upload to Telegram">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                  <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm4.64 6.8c-.15 1.58-.8 5.42-1.13 7.19-.14.75-.42 1-.68 1.03-.58.05-1.02-.38-1.58-.75-.88-.58-1.38-.94-2.23-1.5-.99-.65-.35-1.01.22-1.59.15-.15 2.71-2.48 2.76-2.69a.2.2 0 0 0-.05-.18c-.06-.05-.14-.03-.21-.02-.09.02-1.49.95-4.22 2.79-.4.27-.76.41-1.08.4-.36-.01-1.04-.2-1.55-.37-.63-.2-1.12-.31-1.08-.66.02-.18.27-.36.75-.55 2.92-1.27 4.86-2.11 5.83-2.51 2.78-1.16 3.35-1.36 3.73-1.36.08 0 .27.02.39.12.1.08.13.19.14.27-.01.06.01.24 0 .38z"/>
                </svg>
                Upload to TG
              </button>
            ` : '')}
            ${isCompleted && task.download_url ? `
              <a href="${task.download_url}" class="btn-download-file" download="${escapeHtml(task.friendly_filename || 'video.mp4')}">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                  <polyline points="7 10 12 15 17 10"></polyline>
                  <line x1="12" y1="15" x2="12" y2="3"></line>
                </svg>
                Download File
              </a>
            ` : ''}
            <button class="btn-task-del" onclick="deleteTask('${task.id}')" title="Delete from list">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <line x1="18" y1="6" x2="6" y2="18"></line>
                <line x1="6" y1="6" x2="18" y2="18"></line>
              </svg>
            </button>
          </div>
        </div>
      </div>
    `;

  }).join('');

  container.innerHTML = cardsHtml;
}

// --- Toast Alerts ---
function showToast(message, type = 'info') {
  const container = document.getElementById('toastContainer');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;

  let icon = `
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <circle cx="12" cy="12" r="10"></circle>
      <line x1="12" y1="16" x2="12" y2="12"></line>
      <line x1="12" y1="8" x2="12.01" y2="8"></line>
    </svg>
  `;
  if (type === 'success') {
    icon = `
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--accent-emerald)" stroke-width="2">
        <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path>
        <polyline points="22 4 12 14.01 9 11.01"></polyline>
      </svg>
    `;
  } else if (type === 'error') {
    icon = `
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--accent-rose)" stroke-width="2">
        <circle cx="12" cy="12" r="10"></circle>
        <line x1="15" y1="9" x2="9" y2="15"></line>
        <line x1="9" y1="9" x2="15" y2="15"></line>
      </svg>
    `;
  }

  toast.innerHTML = `${icon} <span>${escapeHtml(message)}</span>`;
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateX(30px)';
    toast.style.transition = 'all 0.3s ease';
    setTimeout(() => toast.remove(), 300);
  }, 3200);
}

// Utility: Escape HTML
function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

async function uploadTaskToYouTube(taskId) {
  try {
    showToast('Starting YouTube upload...', 'info');
    const res = await fetch(`/api/youtube/upload/${taskId}`, { method: 'POST' });
    const data = await res.json();
    if (res.ok) {
      showToast('YouTube upload started in background!', 'success');
      fetchQueue();
    } else {
      showToast(data.detail || 'Upload failed to start.', 'error');
    }
  } catch (e) {
    showToast('Failed to connect to server.', 'error');
  }
}

async function uploadTaskToTelegram(taskId) {
  const cfg = getTelegramConfig();
  if (!cfg.bot_token || !cfg.api_id || !cfg.api_hash) {
    showToast('Please configure Telegram credentials first!', 'error');
    openTgModal();
    return;
  }

  const targetChat = prompt('Enter Telegram Channel/Chat ID (@username or -100...):', cfg.default_chat || '');
  if (!targetChat) return;

  try {
    showToast('Starting Telegram upload...', 'info');
    const res = await fetch(`/api/telegram/upload/${taskId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        bot_token: cfg.bot_token,
        api_id: cfg.api_id,
        api_hash: cfg.api_hash,
        chat_id: targetChat.trim()
      })
    });
    const data = await res.json();
    if (res.ok) {
      showToast('Telegram upload started in background!', 'success');
      fetchQueue();
    } else {
      showToast(data.detail || 'Upload failed to start.', 'error');
    }
  } catch (e) {
    showToast('Failed to connect to server.', 'error');
  }
}

async function cleanDiskStorage() {
  if (!confirm("Are you sure you want to clean your disk storage? Your downloaded video files will be permanently deleted to free space.")) {
    return;
  }
  try {
    showToast('Cleaning your downloaded files...', 'info');
    const uid = getUserId();
    const res = await fetch(`/api/storage/clean?user_id=${encodeURIComponent(uid)}`, { method: 'POST' });
    const data = await res.json();
    if (res.ok && data.success) {
      showToast(`Cleaned! Freed ${data.freed_space} (${data.deleted_files} files deleted)`, 'success');
      await fetchStats();
      await fetchQueue();
    } else {
      showToast('Failed to clean disk storage.', 'error');
    }
  } catch (err) {
    showToast('Network error while cleaning storage.', 'error');
  }
}
