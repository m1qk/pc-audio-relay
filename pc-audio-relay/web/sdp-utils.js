/* Pure SDP helpers shared by the page and by the Node tests. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.PCAudioSdp = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  /* Browsers decode Opus as MONO unless the SDP says stereo=1, even though
     the payload is always "opus/48000/2". Add stereo=1;sprop-stereo=1 to the
     Opus fmtp line (creating one if needed). */
  function addOpusStereo(sdp) {
    var lines = sdp.split(/\r?\n/).filter(function (l) { return l.length > 0; });
    var pts = [];
    lines.forEach(function (l) {
      var m = /^a=rtpmap:(\d+) opus\/48000\/2/i.exec(l);
      if (m) pts.push(m[1]);
    });
    pts.forEach(function (pt) {
      var found = false;
      for (var i = 0; i < lines.length; i++) {
        if (lines[i].indexOf('a=fmtp:' + pt + ' ') === 0) {
          found = true;
          var params = lines[i].slice(lines[i].indexOf(' ') + 1);
          ['stereo=1', 'sprop-stereo=1'].forEach(function (p) {
            var key = p.split('=')[0];
            if (!new RegExp('(^|;)\\s*' + key + '=').test(params)) params += ';' + p;
          });
          lines[i] = 'a=fmtp:' + pt + ' ' + params;
        }
      }
      if (!found) {
        for (var j = 0; j < lines.length; j++) {
          if (lines[j].toLowerCase().indexOf('a=rtpmap:' + pt + ' opus') === 0) {
            lines.splice(j + 1, 0, 'a=fmtp:' + pt + ' minptime=10;stereo=1;sprop-stereo=1');
            break;
          }
        }
      }
    });
    return lines.join('\r\n') + '\r\n';
  }

  /* Average jitter-buffer delay (ms) between two getStats() samples. */
  function intervalBufferMs(prev, cur) {
    if (!prev || !cur) return null;
    var dc = (cur.jitterBufferEmittedCount || 0) - (prev.jitterBufferEmittedCount || 0);
    var dd = (cur.jitterBufferDelay || 0) - (prev.jitterBufferDelay || 0);
    if (dc > 0) return (dd / dc) * 1000;
    return null;
  }

  return { addOpusStereo: addOpusStereo, intervalBufferMs: intervalBufferMs };
});
