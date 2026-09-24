'use strict';

function rectOK(rect) {
  return rect && ['x','y','width','height'].every(key => Number.isFinite(rect[key]))
    && Math.abs(rect.x) <= 100000 && Math.abs(rect.y) <= 100000
    && rect.width > 0 && rect.height > 0 && rect.width <= 30000 && rect.height <= 30000;
}
const clamp = (value, minimum, maximum) => Math.max(minimum, Math.min(value, maximum));

function placePanel({target, workArea, size, mode='dock', floating, gap=8, allowOverlap=false}) {
  if (!rectOK(target) || !rectOK(workArea) || !size || !Number.isFinite(size.width)
      || !Number.isFinite(size.height) || size.width <= 0 || size.height <= 0
      || !['dock','float'].includes(mode) || !Number.isFinite(gap) || gap < 0 || gap > 64) {
    throw new TypeError('Invalid placement dimensions');
  }
  const edge = workArea.x + workArea.width;
  const bottom = workArea.y + workArea.height;
  let width = Math.min(size.width, workArea.width);
  let height = Math.min(size.height, workArea.height);
  let x, y = target.y, side = 'floating', collapsed = false;
  if (mode === 'float') {
    x = Number.isFinite(floating?.x) ? floating.x : target.x + target.width + gap;
    y = Number.isFinite(floating?.y) ? floating.y : target.y;
  } else if (target.x + target.width + gap + width <= edge) {
    x = target.x + target.width + gap; side = 'right';
  } else if (target.x - gap - width >= workArea.x) {
    x = target.x - gap - width; side = 'left';
  } else {
    collapsed = !allowOverlap;
    if (collapsed) { width = Math.min(156, workArea.width); height = Math.min(48, workArea.height); }
    x = Math.min(target.x + target.width, edge) - width - gap;
    y = target.y + (collapsed ? 68 : 8);
    side = 'inset';
  }
  return {x:Math.round(clamp(x,workArea.x,edge-width)),y:Math.round(clamp(y,workArea.y,bottom-height)),
    width:Math.round(width),height:Math.round(height),side,collapsed};
}

function nearEdge(panel, target, distance=28) {
  if (!rectOK(panel) || !rectOK(target)) return false;
  return panel.y < target.y+target.height && panel.y+panel.height > target.y
    && (Math.abs(panel.x-(target.x+target.width+8)) <= distance
      || Math.abs(panel.x+panel.width-(target.x-8)) <= distance);
}

function validFrame(frame) {
  if (!frame || typeof frame !== 'object' || frame.version !== 1
      || !['bound','waiting','ambiguous','error'].includes(frame.state)
      || !['visible','minimized','foreground'].every(key => typeof frame[key] === 'boolean')
      || !Number.isSafeInteger(frame.foreground_pid) || frame.foreground_pid < 0
      || !Number.isSafeInteger(frame.candidate_count) || frame.candidate_count < 0 || frame.candidate_count > 10000) return false;
  return frame.state !== 'bound' || (typeof frame.target === 'string' && /^[a-zA-Z0-9:_-]{1,128}$/.test(frame.target)
    && rectOK(frame.rect) && frame.candidate_count === 1);
}
module.exports = {placePanel, nearEdge, validFrame};
