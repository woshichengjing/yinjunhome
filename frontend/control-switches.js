(function (root) {
  'use strict';

  function createClient(options) {
    var request = options.fetch;
    var now = options.now || Date.now;
    var wait = options.wait || function (ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); };
    var changed = options.changed || function () {};
    var snapshot = null, loadedAt = 0, reading = null, pending = {};

    async function json(url, init) {
      var abort = new AbortController();
      var timer = setTimeout(function () { abort.abort(); }, options.timeoutMs || 8000);
      try {
        var response = await request(url, Object.assign({ cache: 'no-store', signal: abort.signal }, init));
        if (!response.ok) throw new Error('操作未成功（HTTP ' + response.status + '），开关未确认变更');
        var data = await response.json();
        if (!data || typeof data !== 'object' || data.error || data.success === false) {
          throw new Error('服务未确认操作，开关未确认变更');
        }
        return data;
      } catch (error) {
        if (abort.signal.aborted) throw new Error('请求超时，开关变更尚未确认');
        throw error;
      } finally {
        clearTimeout(timer);
      }
    }

    function value(device, flag) {
      var control = snapshot && snapshot.devices && snapshot.devices[device] && snapshot.devices[device].control_switches;
      if (!control || typeof control.observed_at !== 'number' ||
          Math.abs(now() / 1000 - control.observed_at) > 180 || typeof control[flag] !== 'boolean') return null;
      return control[flag];
    }

    async function refresh(force) {
      if (reading) return reading;
      if (!force && loadedAt && now() - loadedAt < 5000) return snapshot;
      reading = (async function () {
        try {
          snapshot = await json('/static/data/state_device_protection.json?t=' + now());
          loadedAt = now();
          return snapshot;
        } catch (error) {
          snapshot = null;
          throw error;
        } finally {
          reading = null;
          changed();
        }
      })();
      return reading;
    }

    function isPending(device, flag) { return !!pending[device + ':' + flag]; }

    function change(device, flag, disabled, url) {
      var key = device + ':' + flag;
      if (pending[key]) return pending[key];
      pending[key] = (async function () {
        try {
          await refresh(true).catch(function () {});
          var before = snapshot && snapshot.devices && snapshot.devices[device] && snapshot.devices[device].control_switches;
          var observed = before && typeof before.observed_at === 'number' ? before.observed_at : -Infinity;
          await json(url, { method: 'POST' });
          // 接口接收不等于控制器采用；等待控制器自己发布的新一轮开关回读。
          for (var i = 0; i < (options.attempts || 30); i++) {
            await wait(options.interval || 2500);
            await refresh(true).catch(function () {});
            var control = snapshot && snapshot.devices && snapshot.devices[device] && snapshot.devices[device].control_switches;
            if (control && control.observed_at > observed && value(device, flag) === disabled) return disabled;
          }
          throw new Error('温控程序尚未确认开关变更，请检查控制接口与温控程序的状态同步');
        } finally {
          delete pending[key];
          changed();
        }
      })();
      changed();
      return pending[key];
    }

    return { refresh: refresh, value: value, change: change, isPending: isPending };
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = { createClient: createClient };
  else root.ControlSwitches = createClient({
    fetch: root.fetch.bind(root),
    changed: function () { root.dispatchEvent(new Event('control-switches-updated')); }
  });
})(typeof window !== 'undefined' ? window : globalThis);
