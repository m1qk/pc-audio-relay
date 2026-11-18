/* PC Audio web receiver.
 *
 * Protocol (see docs/PROTOCOL.md): recvonly audio offer -> POST api/offer ->
 * apply answer. Same-origin, LAN only: no STUN/TURN, no cloud.
 */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var params = new URLSearchParams(location.search);
  var KEY = params.get('k') || '';
  var CONNECT_TIMEOUT_MS = 10000;
  var STALL_MS = 2500;  // silence is sent continuously, so 2.5 s without packets means a real break

  var el = {
    status: $('status'), statusText: $('statusText'), detail: $('detail'),
    btn: $('connectBtn'), resume: $('resumeBtn'), player: $('player'), host: $('host'),
    volume: $('volume'), volRow: $('volRow'), volHint: $('volHint'),
    jb: $('jb'), jbVal: $('jbVal'), jbNote: $('jbNote'), awake: $('awake'),
    rtt: $('stRtt'), buf: $('stBuf'), queue: $('stQueue'), loss: $('stLoss'),
    conc: $('stConc'), est: $('stEst')
  };

  var stream = new MediaStream();   // persistent: play() is unlocked once, tracks come and go
  var pc = null, sid = null;
  var desired = false;              // the user wants to be connected
  var attempt = 0;
  var retryTimer = null, connectTimer = null, disconnectTimer = null, statsTimer = null;
  var lastInbound = null, lastPackets = -1, lastPacketsAt = 0;
  var serverQueueMs = null, lastStatusPoll = 0;
  var wakeLock = null;
  var needsReconnectOnVisible = false;

  function store(key, val) { try { localStorage.setItem(key, String(val)); } catch (e) {} }
  function load(key, dflt) { try { var v = localStorage.getItem(key); return v === null ? dflt : v; } catch (e) { return dflt; } }

  /* ------------------------------ status UI ------------------------------ */
  var LABELS = { idle: 'Not connected', connecting: 'Connecting…', connected: 'Connected',
                 reconnecting: 'Reconnecting…', error: 'Problem' };
  function setStatus(state, detail, isError) {
    el.status.dataset.state = state;
    el.statusText.textContent = LABELS[state] || state;
    el.detail.textContent = detail || '';
    el.detail.className = 'detail' + (isError ? ' err' : '');
    var on = desired;
    el.btn.textContent = on ? 'Disconnect' : 'Connect';
    el.btn.classList.toggle('on', on);
    document.title = state === 'connected' ? 'PC Audio ▶' : 'PC Audio';
  }

  /* ------------------------------ audio unlock --------------------------- */
  function unlockAudio() {
    // Must run inside the tap. Safari only allows audio.play() that is tied to
    // a user gesture; a persistent MediaStream lets later tracks play without
    // another gesture.
    try { if (navigator.audioSession) navigator.audioSession.type = 'playback'; } catch (e) {}
    el.player.srcObject = stream;
    el.player.muted = false;
    var p = el.player.play();
    if (p && p.catch) p.catch(function () {});
  }

  function tryPlay() {
    var p = el.player.play();
    if (p && p.then) {
      p.then(function () { el.resume.hidden = true; })
       .catch(function () { el.resume.hidden = false; });
    }
  }

  /* ------------------------------ peer lifecycle ------------------------- */
  function clearTimers() {
    clearTimeout(retryTimer); clearTimeout(connectTimer); clearTimeout(disconnectTimer);
    retryTimer = connectTimer = disconnectTimer = null;
  }

  function closePeer() {
    var old = pc; pc = null;
    if (old) {
      old.ontrack = old.onconnectionstatechange = null;
      try { old.close(); } catch (e) {}
    }
    stream.getTracks().forEach(function (t) { stream.removeTrack(t); });
    stopStats();
  }

  function waitForIce(p, ms) {
    return new Promise(function (resolve) {
      if (p.iceGatheringState === 'complete') return resolve();
      var done = function () { p.removeEventListener('icegatheringstatechange', check); clearTimeout(t); resolve(); };
      var check = function () { if (p.iceGatheringState === 'complete') done(); };
      var t = setTimeout(done, ms);
      p.addEventListener('icegatheringstatechange', check);
    });
  }

  function RelayError(message, fatal) { this.message = message; this.fatal = !!fatal; }

  async function openPeer() {
    clearTimers();
    closePeer();
    setStatus(attempt === 0 ? 'connecting' : 'reconnecting',
              attempt === 0 ? 'Contacting your PC…' : 'Attempt ' + (attempt + 1) + '…');
    var me = pc = new RTCPeerConnection({ iceServers: [] });  // LAN only
    me.addTransceiver('audio', { direction: 'recvonly' });
    me.ontrack = function (ev) {
      stream.getTracks().forEach(function (t) { stream.removeTrack(t); });
      stream.addTrack(ev.track);
      applyJitterTarget();
      tryPlay();
    };
    me.onconnectionstatechange = function () { onPcState(me); };
    try {
      var offer = await me.createOffer();
      offer.sdp = PCAudioSdp.addOpusStereo(offer.sdp);
      await me.setLocalDescription(offer);
      await waitForIce(me, 1500);
      if (me !== pc) return;
      var res = await fetch('api/offer', {
        method: 'POST', cache: 'no-store',
        headers: { 'Content-Type': 'application/json', 'X-Relay-Key': KEY },
        body: JSON.stringify({ sdp: me.localDescription.sdp, type: me.localDescription.type })
      });
      if (me !== pc) return;
      var body = {};
      try { body = await res.json(); } catch (e) {}
      if (!res.ok) {
        throw new RelayError(res.status === 401
          ? 'The PC rejected the pairing key. Scan the QR code on the PC again.'
          : (body.message || 'The PC returned an error (' + res.status + ').'), res.status === 401);
      }
      sid = body.sessionId;
      await me.setRemoteDescription({ type: 'answer', sdp: body.sdp });
      connectTimer = setTimeout(function () { onConnectTimeout(me); }, CONNECT_TIMEOUT_MS);
    } catch (e) {
      if (me === pc) onOpenFailed(e);
    }
  }

  function onOpenFailed(e) {
    if (e instanceof RelayError) {
      if (e.fatal) { desired = false; closePeer(); setStatus('error', e.message, true); return; }
      scheduleReconnect(e.message); return;
    }
    scheduleReconnect("Can't reach the PC. Is the sender running, and is this iPhone on the same Wi-Fi?");
  }

  function onPcState(p) {
    if (p !== pc) return;
    var s = p.connectionState;
    if (s === 'connected') {
      clearTimeout(connectTimer); clearTimeout(disconnectTimer);
      attempt = 0;
      setStatus('connected', 'Playing your PC\u2019s audio.');
      startStats(); updateMediaSession(); acquireWakeLock(); tryPlay();
    } else if (s === 'disconnected') {
      // Often a brief Wi-Fi hiccup; give it a moment to recover on its own.
      setStatus('reconnecting', 'Connection interrupted…');
      clearTimeout(disconnectTimer);
      disconnectTimer = setTimeout(function () { if (p === pc && p.connectionState !== 'connected') scheduleReconnect('Connection lost.'); }, 3000);
    } else if (s === 'failed' || s === 'closed') {
      if (desired) scheduleReconnect('Connection lost.');
    }
  }

  async function onConnectTimeout(p) {
    if (p !== pc || p.connectionState === 'connected') return;
    var why = 'The PC answered, but the audio connection did not complete. ';
    try {
      var r = await fetch('api/status?sid=' + encodeURIComponent(sid || ''), { cache: 'no-store' });
      var j = await r.json();
      if (j.peer && j.peer.reason) why = j.peer.reason;
      else why += 'Check Windows Firewall (allow the app on Private networks) and that the PC and phone are on the same network.';
    } catch (e) { why += 'Then check that the sender is still running.'; }
    scheduleReconnect(why);
  }

  function scheduleReconnect(why) {
    if (!desired) return;
    closePeer();
    clearTimers();
    attempt += 1;
    var delay = attempt === 1 ? 250 : Math.min(8000, 1000 * Math.pow(2, attempt - 2));  // quick first retry, then back off
    setStatus('reconnecting', why + (delay >= 1000 ? ' Retrying in ' + Math.round(delay / 1000) + ' s…' : ' Retrying…'), attempt > 2);
    if (document.hidden) { needsReconnectOnVisible = true; return; }
    retryTimer = setTimeout(function () { if (desired) openPeer(); }, delay);
  }

  function connect() {
    desired = true; attempt = 0; needsReconnectOnVisible = false;
    unlockAudio();
    openPeer();
  }

  function disconnect() {
    desired = false; needsReconnectOnVisible = false;
    clearTimers();
    hangup();
    closePeer();
    releaseWakeLock();
    el.resume.hidden = true;
    setStatus('idle', '');
  }

  function hangup() {
    if (!sid) return;
    try {
      fetch('api/hangup', { method: 'POST', keepalive: true, body: JSON.stringify({ sessionId: sid }) }).catch(function () {});
    } catch (e) {}
    sid = null;
  }

  /* ------------------------------ buffer / latency ----------------------- */
  var jbSupported = !!(window.RTCRtpReceiver && 'jitterBufferTarget' in RTCRtpReceiver.prototype);
  var hintSupported = !!(window.RTCRtpReceiver && 'playoutDelayHint' in RTCRtpReceiver.prototype);

  function applyJitterTarget() {
    var ms = parseInt(el.jb.value, 10) || 0;
    el.jbVal.textContent = ms + ' ms';
    if (!pc) return;
    pc.getReceivers().forEach(function (r) {
      if (!r.track || r.track.kind !== 'audio') return;
      try {
        if (jbSupported) r.jitterBufferTarget = ms;
        else if (hintSupported) r.playoutDelayHint = ms / 1000;
      } catch (e) {}
    });
  }

  /* ------------------------------ stats ---------------------------------- */
  function fmt(v, unit, digits) { return (v === null || v === undefined || isNaN(v)) ? '–' : v.toFixed(digits || 0) + ' ' + unit; }

  function startStats() { stopStats(); statsTimer = setInterval(pollStats, 1000); pollStats(); }
  function stopStats() { clearInterval(statsTimer); statsTimer = null; lastInbound = null; lastPackets = -1; }

  async function pollStats() {
    var p = pc;
    if (!p || p.connectionState !== 'connected') return;
    var inbound = null, pair = null;
    try {
      (await p.getStats()).forEach(function (r) {
        if (r.type === 'inbound-rtp' && (r.kind === 'audio' || r.mediaType === 'audio')) inbound = r;
        if (r.type === 'candidate-pair' && r.nominated && r.state === 'succeeded') pair = r;
      });
    } catch (e) { return; }
    if (p !== pc || !inbound) return;

    var now = Date.now();
    var rttMs = pair && pair.currentRoundTripTime != null ? pair.currentRoundTripTime * 1000 : null;
    var bufMs = PCAudioSdp.intervalBufferMs(lastInbound, inbound);
    var dRecv = lastInbound ? inbound.packetsReceived - lastInbound.packetsReceived : 0;
    var dLost = lastInbound ? (inbound.packetsLost || 0) - (lastInbound.packetsLost || 0) : 0;
    var lossPct = (dRecv + dLost) > 0 ? 100 * dLost / (dRecv + dLost) : null;
    var concPct = null;
    if (lastInbound && inbound.concealedSamples != null) {
      var dTot = inbound.totalSamplesReceived - lastInbound.totalSamplesReceived;
      if (dTot > 0) concPct = 100 * (inbound.concealedSamples - lastInbound.concealedSamples) / dTot;
    }
    el.rtt.textContent = fmt(rttMs, 'ms', 1);
    el.buf.textContent = fmt(bufMs, 'ms');
    el.loss.textContent = fmt(lossPct, '%', 1);
    el.conc.textContent = fmt(concPct, '%', 1);
    if (serverQueueMs !== null && bufMs !== null) {
      el.est.textContent = '≈ ' + Math.round(serverQueueMs + 20 + (rttMs || 0) / 2 + bufMs + 10) + ' ms';
    }
    window.__pcaudio = { state: el.status.dataset.state, packets: inbound.packetsReceived,
      bytes: inbound.bytesReceived, bufMs: bufMs, rttMs: rttMs, lossPct: lossPct,
      concealedPct: concPct, totalSamples: inbound.totalSamplesReceived,
      audioLevel: inbound.audioLevel, totalEnergy: inbound.totalAudioEnergy, serverQueueMs: serverQueueMs };

    // Media stalled while the connection claims to be fine (sender restarted, phone woke up).
    if (inbound.packetsReceived !== lastPackets) { lastPackets = inbound.packetsReceived; lastPacketsAt = now; }
    else if (now - lastPacketsAt > STALL_MS && desired) { scheduleReconnect('Audio stopped arriving.'); return; }
    lastInbound = inbound;

    if (el.player.paused && desired) { tryPlay(); }
    if (now - lastStatusPoll > 3000) { lastStatusPoll = now; pollServer(); }
  }

  async function pollServer() {
    try {
      var r = await fetch('api/status?sid=' + encodeURIComponent(sid || ''), { cache: 'no-store' });
      var j = await r.json();
      serverQueueMs = j.capture ? j.capture.queuedMs : null;
      el.queue.textContent = fmt(serverQueueMs, 'ms', 0);
    } catch (e) {}
  }

  async function loadInfo() {
    try {
      var r = await fetch('api/info', { cache: 'no-store' });
      var j = await r.json();
      el.host.textContent = j.device ? j.host + ' · ' + j.device : j.host;
    } catch (e) { el.host.textContent = ''; }
  }

  /* ------------------------------ lock screen, wake lock ----------------- */
  function updateMediaSession() {
    if (!('mediaSession' in navigator)) return;
    try {
      navigator.mediaSession.metadata = new MediaMetadata({ title: 'PC audio', artist: (el.host.textContent || 'Windows PC').split(' · ')[0], album: 'PC Audio Relay' });
      navigator.mediaSession.playbackState = 'playing';
      navigator.mediaSession.setActionHandler('play', function () { el.player.play(); });
      navigator.mediaSession.setActionHandler('pause', function () { el.player.pause(); });
    } catch (e) {}
  }

  async function acquireWakeLock() {
    if (!el.awake.checked || !('wakeLock' in navigator) || wakeLock || document.hidden) return;
    try {
      wakeLock = await navigator.wakeLock.request('screen');
      wakeLock.addEventListener('release', function () { wakeLock = null; });
    } catch (e) { wakeLock = null; }
  }
  function releaseWakeLock() { if (wakeLock) { try { wakeLock.release(); } catch (e) {} wakeLock = null; } }

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) return;
    acquireWakeLock();
    if (!desired) return;
    if (needsReconnectOnVisible || !pc || pc.connectionState !== 'connected') {
      needsReconnectOnVisible = false; attempt = 0; openPeer();
    } else if (el.player.paused) { tryPlay(); }
  });
  window.addEventListener('pageshow', function (e) { if (e.persisted && desired) { attempt = 0; openPeer(); } });
  window.addEventListener('pagehide', function () {
    if (sid && navigator.sendBeacon) { try { navigator.sendBeacon('api/hangup', JSON.stringify({ sessionId: sid })); } catch (e) {} }
  });
  window.addEventListener('online', function () { if (desired && (!pc || pc.connectionState !== 'connected')) { attempt = 0; openPeer(); } });

  /* ------------------------------ wiring --------------------------------- */
  el.btn.addEventListener('click', function () { desired ? disconnect() : connect(); });
  el.resume.addEventListener('click', function () { unlockAudio(); tryPlay(); });

  el.volume.value = load('vol', '100');
  el.volume.addEventListener('input', function () { el.player.volume = el.volume.value / 100; store('vol', el.volume.value); });
  (function volumeSupport() {
    var a = document.createElement('audio'); a.volume = 0.5;
    if (a.volume !== 0.5) { el.volRow.hidden = true; el.volHint.hidden = false; }  // iOS: read-only
    else el.player.volume = el.volume.value / 100;
  })();

  el.jb.value = load('jb', '60');
  el.jb.addEventListener('input', function () { store('jb', el.jb.value); applyJitterTarget(); });
  el.jbNote.textContent = jbSupported || hintSupported
    ? 'Lower = less delay but more risk of crackles on weak Wi-Fi. Takes effect immediately.'
    : "This browser doesn't allow changing the receive buffer, so it uses its own default.";
  el.jb.disabled = !(jbSupported || hintSupported);
  applyJitterTarget();

  if (!('wakeLock' in navigator)) {  // Wake Lock needs HTTPS; plain-HTTP LAN pages don't get it
    el.awake.checked = false;
    el.awake.parentNode.hidden = true;
  }
  el.awake.checked = el.awake.parentNode.hidden ? false : load('awake', '0') === '1';
  el.awake.addEventListener('change', function () { store('awake', el.awake.checked ? 1 : 0); el.awake.checked ? acquireWakeLock() : releaseWakeLock(); });

  el.player.addEventListener('pause', function () { if (desired && !document.hidden && pc) el.resume.hidden = false; });
  el.player.addEventListener('playing', function () { el.resume.hidden = true; });

  if (!window.RTCPeerConnection) {
    el.btn.disabled = true;
    setStatus('error', "This browser doesn't support WebRTC.", true);
  } else {
    setStatus('idle', '');
    loadInfo();
    if (params.get('autoconnect') === '1') connect();
  }
})();
