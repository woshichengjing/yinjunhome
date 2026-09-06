/* Hangzhou forecast from a server-side QWeather snapshot. */
(function () {
  'use strict';
  var panel = document.getElementById('weather-forecast');
  if (!panel) return;
  var zone = 'Asia/Shanghai';
  var cacheKey = 'hangzhou-qweather-forecast-30.31-120.12-v2';
  var refreshMs = 30 * 60 * 1000;
  var url = '/static/data/weather_forecast.json';
  var snapshot = null, updated = 0, mode = 'hourly', failed = false, loading = false;
  function node(tag, className, text) {
    var el = document.createElement(tag);
    if (className) el.className = className;
    if (text != null) el.textContent = text;
    return el;
  }
  function number(value) { return typeof value === 'number' && Number.isFinite(value); }
  function degrees(value) { return number(value) ? Math.round(value) + '°' : '—'; }
  function probability(value) { return number(value) ? Math.round(value) + '%' : '—'; }
  function localDate(date) { return new Intl.DateTimeFormat('sv-SE', {timeZone: zone}).format(date); }
  function localTime(date) { return new Intl.DateTimeFormat('zh-CN', {timeZone: zone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}).format(date); }
  function valid(data) {
    return data && data.hourly && data.daily && Array.isArray(data.hourly.time) &&
      data.hourly.time.length >= 24 && Array.isArray(data.daily.time) && data.daily.time.length >= 7 &&
      data.source === 'qweather' &&
      ['temperature_2m', 'weather_code', 'weather_text', 'precipitation_probability', 'is_day'].every(function (key) {
        return Array.isArray(data.hourly[key]) && data.hourly[key].length === data.hourly.time.length;
      }) && ['temperature_2m_min', 'temperature_2m_max', 'weather_code', 'weather_text', 'precipitation_probability_max'].every(function (key) {
        return Array.isArray(data.daily[key]) && data.daily[key].length === data.daily.time.length;
      });
  }
  var codes = {0:'晴',1:'晴间多云',2:'多云',3:'阴',45:'雾',48:'雾',51:'毛毛雨',53:'毛毛雨',55:'毛毛雨',56:'冻毛毛雨',57:'冻毛毛雨',61:'小雨',63:'中雨',65:'大雨',66:'冻雨',67:'冻雨',71:'小雪',73:'中雪',75:'大雪',77:'雪粒',80:'阵雨',81:'阵雨',82:'强阵雨',85:'阵雪',86:'阵雪',95:'雷阵雨',96:'雷雨冰雹',99:'雷雨冰雹'};
  function icon(code, day, text) {
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    var sun = '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>';
    var cloud = '<path d="M6 17a4 4 0 0 1 0-8 6 6 0 0 1 11-2 5 5 0 0 1 1 10Z"/>';
    var label = text || codes[code] || '天气未知';
    var thunder = /雷/.test(label), snow = /雪|冰粒|冻雨/.test(label);
    var rain = /雨|降水/.test(label), haze = /雾|霾|沙|尘/.test(label);
    var cloudy = /云|阴/.test(label);
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none'); svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.5'); svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('class', 'forecast-icon ' + (rain || thunder ? 'rain' : cloudy || haze ? 'cloud' : ''));
    svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', label);
    if (!number(code)) svg.innerHTML = '<path d="M9 8a3 3 0 1 1 5 2c-2 1-2 2-2 3m0 4v.1"/>';
    else if (thunder) svg.innerHTML = cloud + '<path d="m12 13-3 5h4l-2 4"/>';
    else if (snow) svg.innerHTML = cloud + '<path d="M8 20h.1m5 1h.1m5-1h.1"/>';
    else if (rain) svg.innerHTML = cloud + '<path d="m8 19-1 3m6-3-1 3m6-3-1 3"/>';
    else if (haze) svg.innerHTML = cloud + '<path d="M4 20h16"/>';
    else if (cloudy) svg.innerHTML = cloud;
    else svg.innerHTML = day === 0 ? '<path d="M20 15A9 9 0 0 1 9 4a8 8 0 1 0 11 11Z"/>' : sun;
    return svg;
  }
  var header = node('div', 'forecast-header');
  header.append(node('h2', 'forecast-heading', '天气'), node('span', 'forecast-location', '杭州'));
  var tabs = node('div', 'forecast-tabs'); tabs.setAttribute('role', 'tablist'); tabs.setAttribute('aria-label', '预报时段');
  var content = node('div', 'forecast-content'); content.id = 'forecast-content'; content.setAttribute('role', 'tabpanel');
  ['hourly', 'daily'].forEach(function (value, index) {
    var button = node('button', 'forecast-tab', index === 0 ? '24小时' : '7天');
    button.type = 'button'; button.id = 'forecast-tab-' + value; button.dataset.mode = value;
    button.setAttribute('role', 'tab'); button.setAttribute('aria-controls', content.id);
    button.addEventListener('click', function () { mode = value; render(); });
    button.addEventListener('keydown', function (event) {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      var next = event.key === 'Home' ? 0 : event.key === 'End' ? 1 : 1 - index;
      tabs.children[next].click(); tabs.children[next].focus();
    });
    tabs.appendChild(button);
  });
  panel.appendChild(header);
  // Keep the same live-reading node so existing weather refreshes update it in place.
  var currentWeather = document.getElementById('windy-overlay');
  if (currentWeather) {
    currentWeather.classList.add('forecast-current');
    panel.appendChild(currentWeather);
  }
  panel.append(tabs, content);
  function hourly() {
    var h = snapshot.hourly, start = Math.floor(Date.now() / 3600000) * 3600000;
    return h.time.map(function (time, i) {
      return {time: new Date(time), temp: h.temperature_2m[i], rain: h.precipitation_probability[i], code: h.weather_code[i], text:h.weather_text[i], day: h.is_day[i]};
    }).filter(function (item) { return item.time.getTime() >= start; }).slice(0, 24);
  }
  function empty(text) {
    var message = node('div', 'forecast-message', text);
    if (!loading) {
      message.appendChild(node('br'));
      var retry = node('button', 'forecast-retry', '重新加载'); retry.type = 'button';
      retry.addEventListener('click', function () { load(true); }); message.appendChild(retry);
    }
    content.appendChild(message);
  }
  function renderHourly() {
    var items = hourly();
    if (!items.length) { empty('暂无可用的逐小时预报'); return; }
    var temps = items.map(function (item) { return item.temp; }).filter(number);
    var low = temps.length ? Math.min.apply(null, temps) : null;
    var high = temps.length ? Math.max.apply(null, temps) : null;
    var summary = node('p', 'forecast-summary');
    summary.appendChild(node('strong', '', '最低 ' + degrees(low) + ' · 最高 ' + degrees(high)));
    content.appendChild(summary);
    var scroll = node('div', 'forecast-scroll'); scroll.tabIndex = 0;
    scroll.setAttribute('role', 'region'); scroll.setAttribute('aria-label', '逐小时天气，可左右滚动');
    var labels = node('div', 'forecast-hours'), rain = node('div', 'forecast-rain');
    var width = items.length * 50, span = Math.max(1, high - low), paths = [], path = [], points = [];
    items.forEach(function (item, i) {
      var label = node('div', 'forecast-hour', i === 0 ? '现在' : localTime(item.time));
      label.title = localDate(item.time) + ' ' + localTime(item.time) + ' · ' + (item.text || codes[item.code] || '未知');
      label.appendChild(icon(item.code, item.day, item.text)); labels.appendChild(label);
      rain.appendChild(node('span', '', probability(item.rain)));
      if (!number(item.temp)) { if (path.length) paths.push(path); path = []; return; }
      var x = i * 50 + 25, y = 54 - (item.temp - low) / span * 26;
      path.push(x + ',' + y); points.push({x:x, y:y, temp:item.temp});
    });
    if (path.length) paths.push(path);
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'forecast-curve'); svg.setAttribute('width', width); svg.setAttribute('viewBox', '0 0 ' + width + ' 72');
    svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', '逐小时室外气温曲线，单位摄氏度');
    svg.innerHTML = '<path d="M0 64H' + width + '" stroke="#a5c5e012"/>' +
      paths.map(function (p) { return '<polyline points="' + p.join(' ') + '" fill="none" stroke="#8dbfdb" stroke-width="2" stroke-linejoin="round"/>'; }).join('') +
      points.map(function (p) { return '<circle cx="' + p.x + '" cy="' + p.y + '" r="2.5" fill="#bfdef1"/><text x="' + p.x + '" y="' + (p.y - 12) + '" text-anchor="middle" fill="#e0ebf5" font-size="12">' + degrees(p.temp) + '</text>'; }).join('');
    scroll.append(labels, svg, rain); content.appendChild(scroll);
  }
  function renderDaily() {
    var d = snapshot.daily, today = localDate(new Date());
    var indexes = d.time.map(function (_, i) { return i; }).filter(function (i) { return d.time[i] >= today; }).slice(0, 7);
    if (!indexes.length) { empty('暂无可用的逐日预报'); return; }
    var values = indexes.flatMap(function (i) { return [d.temperature_2m_min[i], d.temperature_2m_max[i]]; }).filter(number);
    var min = Math.min.apply(null, values), max = Math.max.apply(null, values), span = Math.max(1, max - min);
    var list = node('div', 'forecast-days');
    indexes.forEach(function (i) {
      var date = new Date(d.time[i] + 'T12:00:00+08:00');
      var label = d.time[i] === today ? '今天' : new Intl.DateTimeFormat('zh-CN', {timeZone:zone, weekday:'short'}).format(date);
      var row = node('div', 'forecast-day'); row.title = d.time[i];
      var condition = node('span', 'forecast-condition', d.weather_text[i] || codes[d.weather_code[i]] || '未知');
      condition.appendChild(node('small', '', probability(d.precipitation_probability_max[i])));
      var range = node('span', 'forecast-range'), bar = node('span'); range.setAttribute('aria-hidden', 'true');
      var low = d.temperature_2m_min[i], high = d.temperature_2m_max[i];
      if (number(low) && number(high)) { bar.style.left = (low-min)/span*100 + '%'; bar.style.width = (high-low)/span*100 + '%'; range.appendChild(bar); }
      row.append(node('span', 'forecast-day-name', label), icon(d.weather_code[i], 1, d.weather_text[i]), condition, node('span', 'forecast-low', degrees(low)), range, node('span', 'forecast-high', degrees(high)));
      list.appendChild(row);
    });
    content.appendChild(list);
  }
  function render() {
    Array.from(tabs.children).forEach(function (button) {
      var active = button.dataset.mode === mode;
      button.setAttribute('aria-selected', String(active)); button.tabIndex = active ? 0 : -1;
    });
    content.setAttribute('aria-labelledby', 'forecast-tab-' + mode);
    content.replaceChildren();
    if (snapshot) { if (mode === 'hourly') renderHourly(); else renderDaily(); }
    else empty(loading ? '正在获取杭州天气预报…' : '预报暂时无法连接，请稍后重试');
  }
  async function load(force) {
    if (loading || (!force && snapshot && Date.now() - updated < refreshMs)) return;
    loading = true; if (!snapshot) render();
    var abort = new AbortController(), timeout = setTimeout(function () { abort.abort(); }, 15000);
    try {
      var response = await fetch(url + '?t=' + Date.now(), {signal:abort.signal, credentials:'same-origin', cache:'no-store'});
      if (!response.ok) throw new Error('Forecast HTTP ' + response.status);
      var data = await response.json(); if (!valid(data)) throw new Error('Incomplete forecast');
      snapshot = data; updated = Date.parse(data.updated_at) || Date.now(); failed = false;
      try { localStorage.setItem(cacheKey, JSON.stringify({data:data, time:updated})); } catch (_) { /* Storage can be disabled on wall tablets. */ }
    } catch (_) { failed = true; }
    finally { clearTimeout(timeout); loading = false; render(); }
  }
  function fitPanel() {
    var deck = document.querySelector('.container-grid');
    var bottom = deck ? deck.getBoundingClientRect().top - 12 : window.innerHeight - 12;
    panel.style.setProperty('--forecast-free-height', Math.max(0, bottom) + 'px');
  }
  try {
    var saved = JSON.parse(localStorage.getItem(cacheKey));
    if (saved && valid(saved.data) && number(saved.time) && Date.now() - saved.time >= 0 && Date.now() - saved.time < 86400000) {
      snapshot = saved.data; updated = saved.time; failed = Date.now() - updated >= refreshMs;
    }
  } catch (_) { /* Missing or invalid cache is harmless. */ }
  render(); load(false); fitPanel();
  if (window.ResizeObserver) new ResizeObserver(fitPanel).observe(document.querySelector('.container-grid'));
  window.addEventListener('resize', fitPanel);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) load(false); });
  setInterval(function () { if (!document.hidden) load(true); }, refreshMs);
})();
