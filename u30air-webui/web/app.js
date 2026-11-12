let isAuthed = false;
let pollTimer = null;

// 通用 API 调用桥接 (自动处理 401 鉴权)
async function apiCall(method, req = {}) {
  try {
    const resp = await fetch('/api/call', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ method, req })
    });
    if (resp.status === 401) {
      handleAuthRequired();
      return { ok: 0, error: 'unauthorized' };
    }
    return await resp.json();
  } catch (err) {
    console.error('API Error:', method, err);
    return { ok: 0, error: String(err) };
  }
}

function showToast(msg, isSuccess = true) {
  const toast = document.getElementById('toast');
  document.getElementById('toast-text').innerText = msg;
  document.getElementById('toast-icon').innerText = isSuccess ? '✅' : '⚠️';
  toast.classList.remove('translate-y-24', 'opacity-0');
  setTimeout(() => toast.classList.add('translate-y-24', 'opacity-0'), 2800);
}

function _legacyExecCopy(str) {
  try {
    const ta = document.createElement('textarea');
    ta.value = String(str);
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.top = '-9999px';
    ta.style.left = '-9999px';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return !!ok;
  } catch (e) {
    return false;
  }
}

function copyToClipboard(text, label = 'IP 地址') {
  if (!text || text === '--' || text.includes('读取中')) return;
  const done = () => showToast(`已复制 ${label}: ${text}`);
  if (navigator.clipboard && window.isSecureContext && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => {
      if (_legacyExecCopy(text)) done();
      else showToast('复制失败，请手动选中复制', false);
    });
  } else if (_legacyExecCopy(text)) {
    done();
  } else {
    window.prompt(`请复制 ${label}:`, text);
  }
}

// 鉴权系统
async function checkAuthStatus() {
  try {
    const r = await fetch('/api/auth/status');
    const data = await r.json();
    if (data.authenticated) {
      isAuthed = true;
      document.getElementById('login-modal').classList.add('hidden');
      document.getElementById('top-user-name').innerText = data.username || 'ubuntu';
      startPolling();
    } else {
      handleAuthRequired();
    }
  } catch (e) {
    handleAuthRequired();
  }
}

function handleAuthRequired() {
  isAuthed = false;
  stopPolling();
  document.getElementById('login-modal').classList.remove('hidden');
  document.getElementById('login-pass').focus();
}

async function doLogin() {
  const u = document.getElementById('login-user').value.trim();
  const p = document.getElementById('login-pass').value;
  const rem = document.getElementById('login-remember').checked;
  const errEl = document.getElementById('login-error-msg');
  const btn = document.getElementById('btn-login');

  errEl.classList.add('hidden');
  if (!u || !p) {
    errEl.innerText = '请输入账号和密码';
    errEl.classList.remove('hidden');
    return;
  }

  btn.disabled = true;
  btn.innerText = '正在验证凭证...';

  try {
    const resp = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: u, password: p, remember: rem })
    });
    const res = await resp.json();
    if (res.ok) {
      isAuthed = true;
      document.getElementById('login-modal').classList.add('hidden');
      document.getElementById('top-user-name').innerText = res.username || u;
      showToast('登录成功，欢迎回来');
      fetchOverview();
      startPolling();
      requestAnimationFrame(renderTrafficChart);
    } else {
      errEl.innerText = res.error || '用户名或密码不正确';
      errEl.classList.remove('hidden');
    }
  } catch (err) {
    errEl.innerText = '连接服务器失败: ' + String(err);
    errEl.classList.remove('hidden');
  } finally {
    btn.disabled = false;
    btn.innerText = '立即登录';
  }
}

async function doLogout() {
  if (!confirm('确定退出当前登录状态？')) return;
  await fetch('/api/logout', { method: 'POST' });
  showToast('已安全退出登录');
  handleAuthRequired();
}

function togglePwdModalVisibility(show) {
  const t = show ? 'text' : 'password';
  ['pwd-old', 'pwd-new', 'pwd-confirm'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.type = t;
  });
}

function openPasswordModal() {
  const user = (document.getElementById('top-user-name')?.innerText || 'ubuntu').trim();
  const userEl = document.getElementById('pwd-modal-user');
  if (userEl) userEl.innerText = user;
  ['pwd-old', 'pwd-new', 'pwd-confirm'].forEach(id => {
    const el = document.getElementById(id);
    if (el) { el.value = ''; el.type = 'password'; }
  });
  const chk = document.getElementById('pwd-show-plain');
  if (chk) chk.checked = false;
  const errEl = document.getElementById('pwd-error-msg');
  if (errEl) { errEl.innerText = ''; errEl.classList.add('hidden'); }
  document.getElementById('password-modal').classList.remove('hidden');
  setTimeout(() => document.getElementById('pwd-old')?.focus(), 50);
}

function closePasswordModal() {
  ['pwd-old', 'pwd-new', 'pwd-confirm'].forEach(id => {
    const el = document.getElementById(id);
    if (el) { el.value = ''; el.type = 'password'; }
  });
  document.getElementById('password-modal').classList.add('hidden');
}

async function submitPasswordChange() {
  const oldPassword = document.getElementById('pwd-old').value;
  const newPassword = document.getElementById('pwd-new').value;
  const confirmPassword = document.getElementById('pwd-confirm').value;
  const errEl = document.getElementById('pwd-error-msg');
  const btn = document.getElementById('btn-pwd-submit');

  const showErr = (msg) => {
    errEl.innerText = msg;
    errEl.classList.remove('hidden');
  };
  errEl.classList.add('hidden');

  if (!oldPassword) return showErr('请输入当前密码');
  if (newPassword.length < 6) return showErr('新密码长度至少需要 6 位');
  if (newPassword.length > 128) return showErr('新密码长度不能超过 128 位');
  if (newPassword !== confirmPassword) return showErr('两次输入的新密码不一致，请核对');
  if (newPassword === oldPassword) return showErr('新密码不能与当前密码相同');

  btn.disabled = true;
  btn.innerText = '正在同步修改...';
  try {
    const resp = await fetch('/api/auth/password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        old_password: oldPassword,
        new_password: newPassword,
        confirm_password: confirmPassword
      })
    });
    if (resp.status === 401) {
      closePasswordModal();
      handleAuthRequired();
      return;
    }
    const res = await resp.json();
    if (res.ok) {
      const loginPassEl = document.getElementById('login-pass');
      if (loginPassEl) loginPassEl.value = '';
      closePasswordModal();
      showToast(res.msg || '面板与 SSH 系统密码已同步更新');
    } else {
      showErr(res.error || '修改密码失败');
    }
  } catch (err) {
    showErr('请求失败: ' + String(err));
  } finally {
    btn.disabled = false;
    btn.innerText = '确认修改密码';
  }
}

let netTimer = null;
function startPolling() {
  if (!pollTimer) {
    pollTimer = setInterval(fetchOverview, 5000);
  }
  if (!netTimer) {
    netTimer = setInterval(pollRealtimeNet, 1500);
  }
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  if (netTimer) {
    clearInterval(netTimer);
    netTimer = null;
  }
}

// 页面可见性自适应暂停/恢复轮询（省电防发热）
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    stopPolling();
  } else if (isAuthed) {
    fetchOverview();
    startPolling();
  }
});

// Tab 切换逻辑
function switchTab(tabId) {
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.add('hidden'));
  document.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.remove('nav-tab-active');
    b.classList.add('text-slate-400');
  });

  const activePanel = document.getElementById(`panel-${tabId}`);
  if (activePanel) activePanel.classList.remove('hidden');

  const activeBtn = document.querySelector(`[data-tab="${tabId}"]`);
  if (activeBtn) {
    activeBtn.classList.add('nav-tab-active');
    activeBtn.classList.remove('text-slate-400');
  }

  if (tabId === 'overview') requestAnimationFrame(renderTrafficChart);
  if (tabId === 'network') loadNetworkTab();
  if (tabId === 'sms') loadSmsTab();
  if (tabId === 'at') loadAtHistory();
  if (tabId === 'traffic') loadTrafficStats();
  if (tabId === 'wifi') loadWifiTab();
  if (tabId === 'vpn') loadFullVpnData();
  if (tabId === 'power') loadPowerTab();
  if (tabId === 'system') { fetchSysLogs(); checkBluetoothStatus(); loadLogStatus(); loadSysTab(); }
}

// 格式化函数
function formatBytes(bytes) {
  if (!bytes || bytes <= 0) return '0 B';
  const k = 1024, units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return (bytes / Math.pow(k, i)).toFixed(i >= 2 ? 2 : 1) + ' ' + units[i];
}

function formatUptime(seconds) {
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `${d}天 ${h}小时 ${m}分钟`;
}

// 概览数据与实时波形
let lastRx = 0, lastTx = 0, lastTime = Date.now();
let rxHistory = new Array(25).fill(0);
let txHistory = new Array(25).fill(0);
let isFetchingOverview = false;

async function fetchOverview(manual = false) {
  if (!isAuthed) return;
  if (isFetchingOverview && !manual) return;
  isFetchingOverview = true;
  let data = null;
  try {
    data = await apiCall('overview');
  } finally {
    isFetchingOverview = false;
  }
  if (data && data.ok) {
    if (data.device) document.getElementById('top-dev-name').innerText = data.device.toUpperCase();
    if (data.hostname) document.getElementById('top-host').innerText = data.hostname;

    const curVer = data.system_version || data.release || 'v2026.10.26.1';
    const elCurVer = document.getElementById('upd-cur-ver');
    if (elCurVer) elCurVer.innerText = curVer;
    if (data.kernel) {
      const elCurKernel = document.getElementById('upd-cur-kernel');
      if (elCurKernel) elCurKernel.innerText = data.kernel.split('-')[0] + ' (Stable 稳定版)';
    }

    const sig = data.signal || {};
    const qos = data.cellular_qos || {};
    const opName = qos.operator_name || (sig.operator === '46000' ? '中国移动' : (sig.operator === '46001' ? '中国联通' : (sig.operator === '46011' ? '中国电信' : (sig.operator || '蜂窝网络'))));
    const ratStr = qos.rat || sig.rat || '5G SA';
    const bandStr = qos.band || 'n41';
    const qciStr = qos.qci ? ('QCI: ' + qos.qci) : 'QCI: 8';
    const dlRate = qos.dl_rate_str || '512 Mbps';
    const ulRate = qos.ul_rate_str || '60 Mbps';
    const rsrpStr = (qos.rsrp || sig.rsrp || '--') + ' dBm';
    const rsrqStr = (qos.rsrq || sig.rsrq || '--') + ' dB';
    const sinrStr = (qos.sinr !== undefined ? qos.sinr : '8.0') + ' dB';

    // 1. 概览卡片更新
    const elOvOp = document.getElementById('ov-operator');
    if (elOvOp) elOvOp.innerText = opName;
    const elOvRat = document.getElementById('ov-rat');
    if (elOvRat) elOvRat.innerText = ratStr;
    const elOvBand = document.getElementById('ov-band');
    if (elOvBand) elOvBand.innerText = bandStr;
    const elOvQci = document.getElementById('ov-qci');
    if (elOvQci) elOvQci.innerText = qciStr;
    const elOvDl = document.getElementById('ov-dl-rate');
    if (elOvDl) elOvDl.innerText = dlRate;
    const elOvUl = document.getElementById('ov-ul-rate');
    if (elOvUl) elOvUl.innerText = ulRate;
    const elOvRsrp = document.getElementById('ov-rsrp');
    if (elOvRsrp) elOvRsrp.innerText = rsrpStr;
    const elOvSinr = document.getElementById('ov-sinr');
    if (elOvSinr) elOvSinr.innerText = sinrStr;
    const elOvRsrq = document.getElementById('ov-rsrq');
    if (elOvRsrq) elOvRsrq.innerText = rsrqStr;

    // 2. 移动网络卡片更新
    const elNetOpName = document.getElementById('net-operator-name');
    if (elNetOpName) elNetOpName.innerText = opName;
    const elNetRatBadge = document.getElementById('net-rat-badge');
    if (elNetRatBadge) elNetRatBadge.innerText = ratStr;
    const elNetBandBadge = document.getElementById('net-band-badge');
    if (elNetBandBadge) elNetBandBadge.innerText = bandStr;
    const elNetQci = document.getElementById('net-qci');
    if (elNetQci) elNetQci.innerText = qciStr;
    const elNetDl = document.getElementById('net-dl-rate');
    if (elNetDl) elNetDl.innerText = dlRate;
    const elNetUl = document.getElementById('net-ul-rate');
    if (elNetUl) elNetUl.innerText = ulRate;
    const elNetRsrp = document.getElementById('net-rsrp-val');
    if (elNetRsrp) elNetRsrp.innerText = rsrpStr;
    const elNetSinr = document.getElementById('net-sinr-val');
    if (elNetSinr) elNetSinr.innerText = sinrStr;
    const elNetRsrq = document.getElementById('net-rsrq-val');
    if (elNetRsrq) elNetRsrq.innerText = rsrqStr;

    // 信号格动态显示
    const rsrpNum = parseInt(qos.rsrp || sig.rsrp || -95);
    let activeBars = 4;
    if (rsrpNum >= -80) activeBars = 5;
    else if (rsrpNum >= -90) activeBars = 4;
    else if (rsrpNum >= -100) activeBars = 3;
    else if (rsrpNum >= -112) activeBars = 2;
    else activeBars = 1;

    const updateBars = (containerId) => {
      const container = document.getElementById(containerId);
      if (container && container.children) {
        Array.from(container.children).forEach((bar, idx) => {
          if (idx < activeBars) {
            bar.className = bar.className.replace('bg-slate-700', 'bg-cyan-400');
          } else {
            bar.className = bar.className.replace('bg-cyan-400', 'bg-slate-700');
          }
        });
      }
    };
    updateBars('sig-bars');
    updateBars('net-sig-bars');

    if (data.data || data.cellular_ip) {
      const cell = data.cellular_ip || {};
      const dData = data.data || {};
      const localIpv4 = cell.ipv4 || (dData.address ? dData.address.split('/')[0] : '') || dData.ipv4 || '';
      const pubIpv4 = cell.public_ipv4 || dData.public_ipv4 || '';
      const ipv4Display = pubIpv4 || localIpv4 || '--';
      const ipv6Val = cell.ipv6 || dData.ipv6 || '--';

      const elOvIpv4 = document.getElementById('ov-cell-ipv4');
      if (elOvIpv4) {
        elOvIpv4.innerText = ipv4Display;
        if (pubIpv4 && localIpv4 && pubIpv4 !== localIpv4) {
          elOvIpv4.title = `公网出口 IP: ${pubIpv4} (蜂窝内网: ${localIpv4})`;
        }
      }
      const elOvIpv6 = document.getElementById('ov-cell-ipv6');
      if (elOvIpv6) elOvIpv6.innerText = ipv6Val;

      const netIp = document.getElementById('net-ip');
      if (netIp) netIp.innerText = ipv4Display;
      const netLocalIp = document.getElementById('net-local-ip');
      if (netLocalIp) netLocalIp.innerText = localIpv4 || '--';
      const netIpv6 = document.getElementById('net-ipv6');
      if (netIpv6) netIpv6.innerText = ipv6Val;

      const netIface = document.getElementById('net-iface');
      if (netIface) netIface.innerText = cell.iface || dData.iface || 'sipa_eth0';
      const netOp = document.getElementById('net-op');
      if (netOp) netOp.innerText = (sig.operator || '46000') + ' (' + opName + ')';
    }

    // 实时吞吐率更新
    if (data.realtime) {
      updateThroughput(data.realtime.rx_rate || 0, data.realtime.tx_rate || 0);
    } else {
      const tr = (data.traffic && data.traffic.today) ? data.traffic.today : null;
      if (tr) {
        const now = Date.now();
        const dt = (now - lastTime) / 1000;
        if (lastRx > 0 && dt > 0) {
          const rxRate = Math.max(0, (tr.rx - lastRx) / dt);
          const txRate = Math.max(0, (tr.tx - lastTx) / dt);
          updateThroughput(rxRate, txRate);
        }
        lastRx = tr.rx;
        lastTx = tr.tx;
        lastTime = now;
      }
    }

    if (data.traffic) {
      if (data.traffic.today) {
        document.getElementById('ov-today-traffic').innerText = formatBytes((data.traffic.today.rx || 0) + (data.traffic.today.tx || 0));
      }
      if (data.traffic.cycle) {
        const usedBytes = Number(data.traffic.cycle.used || 0);
        document.getElementById('ov-cycle-traffic').innerText = formatBytes(usedBytes);
        document.getElementById('ov-days-left').innerText = (data.traffic.cycle.days_left || 0) + ' 天';
        const capBytes = Number((data.traffic.cap && data.traffic.cap.bytes) || 0);
        const warnPct = Number((data.traffic.cap && data.traffic.cap.warn) || 80);
        const quotaWrap = document.getElementById('ov-quota-bar-wrap');
        const quotaPct = document.getElementById('ov-quota-pct');
        const quotaBar = document.getElementById('ov-quota-bar');
        const alertBanner = document.getElementById('ov-traffic-alert-banner');
        const alertTitle = document.getElementById('ov-traffic-alert-title');
        const alertDesc = document.getElementById('ov-traffic-alert-desc');
        const alertIcon = document.getElementById('ov-traffic-alert-icon');
        if (quotaWrap && quotaPct && quotaBar) {
          if (capBytes > 0) {
            const rawPct = Math.round((usedBytes / capBytes) * 100);
            const pct = Math.min(100, rawPct);
            quotaWrap.classList.remove('hidden');
            quotaPct.innerText = `${rawPct}% (${formatBytes(usedBytes)} / ${formatBytes(capBytes)})`;
            quotaBar.style.width = `${pct}%`;
            quotaBar.className = rawPct >= 95
              ? 'h-full bg-rose-500 rounded-full transition-all'
              : (rawPct >= warnPct ? 'h-full bg-amber-500 rounded-full transition-all' : 'h-full bg-emerald-400 rounded-full transition-all');
            if (alertBanner && alertTitle && alertDesc) {
              if (rawPct >= warnPct) {
                alertBanner.classList.remove('hidden');
                if (rawPct >= 100) {
                  alertBanner.className = 'p-3.5 rounded-2xl bg-rose-500/15 border border-rose-500/40 flex flex-col sm:flex-row sm:items-center justify-between gap-2 text-xs';
                  if (alertIcon) alertIcon.innerText = '🚨';
                  alertTitle.className = 'font-bold text-rose-300';
                  alertTitle.innerText = `月度流量已达上限 (${rawPct}%)`;
                } else {
                  alertBanner.className = 'p-3.5 rounded-2xl bg-amber-500/15 border border-amber-500/40 flex flex-col sm:flex-row sm:items-center justify-between gap-2 text-xs';
                  if (alertIcon) alertIcon.innerText = '⚠️';
                  alertTitle.className = 'font-bold text-amber-300';
                  alertTitle.innerText = `月度流量配额预警：已达 ${rawPct}% (预警线 ${warnPct}%)`;
                }
                alertDesc.innerText = `本计费周期已用 ${formatBytes(usedBytes)} / 限额 ${formatBytes(capBytes)}，距离结算重置还剩 ${data.traffic.cycle.days_left || 0} 天`;
              } else {
                alertBanner.classList.add('hidden');
              }
            }
          } else {
            quotaWrap.classList.add('hidden');
            if (alertBanner) alertBanner.classList.add('hidden');
          }
        }
      }
    }

    const batDet = data.battery_details || {};
    const batCap = batDet.capacity !== undefined ? batDet.capacity : (data.battery ? data.battery.capacity : 90);
    const batVolt = batDet.voltage_v || ((data.battery && data.battery.voltage_uv ? data.battery.voltage_uv / 1000000 : 4.0)).toFixed(2);

    document.getElementById('ov-bat-cap').innerText = batCap + '%';
    document.getElementById('ov-bat-bar').style.width = batCap + '%';
    document.getElementById('ov-bat-volt').innerText = batVolt + ' V';
    if (batDet.temp_c !== undefined && document.getElementById('ov-bat-temp')) {
      document.getElementById('ov-bat-temp').innerText = Number(batDet.temp_c).toFixed(1) + '°C';
    }

    const elBatStatus = document.getElementById('ov-bat-status');
    const elBatBadge = document.getElementById('ov-bat-badge');
    renderBatteryStatus(batDet, 'ov');
    const elBatPlugged = document.getElementById('ov-bat-plugged');
    if (elBatPlugged) {
      elBatPlugged.innerText = batDet.plugged === true ? (batDet.charging ? 'Type-C 充电中' : 'Type-C 供电中') : (batDet.plugged === false ? '纯电池运行' : '供电来源未知');
    }

    const thm = data.thermal_details || (batDet && batDet.thermal) || null;
    if (thm) {
      const socTemp = Number(thm.soc || thm.max_temp || 45.0);
      document.getElementById('ov-temp').innerText = socTemp.toFixed(1) + '°C';
      document.getElementById('ov-temp-bar').style.width = Math.min(100, Math.max(12, (socTemp / 85) * 100)) + '%';
      if (document.getElementById('ov-temp-board') && thm.board !== undefined) {
        document.getElementById('ov-temp-board').innerText = Number(thm.board).toFixed(1) + '°C';
      }
      if (document.getElementById('ov-temp-cpu') && thm.cpu_big !== undefined) {
        document.getElementById('ov-temp-cpu').innerText = Number(thm.cpu_big).toFixed(1) + '°C';
      }
      if (document.getElementById('ov-temp-modem') && thm.modem !== undefined) {
        document.getElementById('ov-temp-modem').innerText = Number(thm.modem).toFixed(1) + '°C';
      }
      if (document.getElementById('ov-temp-pa') && thm.pa !== undefined) {
        document.getElementById('ov-temp-pa').innerText = Number(thm.pa).toFixed(1) + '°C';
      }
      if (document.getElementById('ov-temp-bat') && thm.battery !== undefined) {
        document.getElementById('ov-temp-bat').innerText = Number(thm.battery).toFixed(1) + '°C';
      }
      const tBadge = document.getElementById('ov-temp-badge');
      if (tBadge) {
        tBadge.innerText = thm.level_text || '良好';
        if (thm.level === 'cool' || thm.level === 'normal') {
          tBadge.className = 'text-[10px] text-emerald-400 font-semibold px-2 py-0.5 bg-emerald-500/15 border border-emerald-500/30 rounded-full';
        } else if (thm.level === 'warm') {
          tBadge.className = 'text-[10px] text-amber-400 font-semibold px-2 py-0.5 bg-amber-500/15 border border-amber-500/30 rounded-full';
        } else {
          tBadge.className = 'text-[10px] text-rose-400 font-semibold px-2 py-0.5 bg-rose-500/15 border border-rose-500/30 rounded-full animate-pulse';
        }
      }
    } else if (data.temperatures && data.temperatures.length) {
      const maxTemp = Math.max(...data.temperatures.map(t => t.millicelsius || 0)) / 1000;
      document.getElementById('ov-temp').innerText = maxTemp.toFixed(1) + '°C';
      document.getElementById('ov-temp-bar').style.width = Math.min(100, Math.max(10, (maxTemp / 90) * 100)) + '%';
    }

    if (data.memory) {
      const totalM = (data.memory.total_kb / 1024 / 1024).toFixed(1);
      const availM = (data.memory.avail_kb / 1024 / 1024).toFixed(1);
      const usedPct = Math.round((1 - (data.memory.avail_kb / data.memory.total_kb)) * 100);
      document.getElementById('ov-ram-txt').innerText = `${availM}G 剩余 / ${totalM}G`;
      document.getElementById('ov-ram-bar').style.width = `${usedPct}%`;
    }
    if (data.storage && data.storage.root) {
      const sz = (data.storage.root.size / 1024 / 1024).toFixed(1);
      const av = (data.storage.root.avail / 1024 / 1024).toFixed(1);
      const pct = Math.round((data.storage.root.used / data.storage.root.size) * 100);
      document.getElementById('ov-disk-txt').innerText = `${av}G 剩余 / ${sz}G`;
      document.getElementById('ov-disk-bar').style.width = `${pct}%`;
    }

    if (data.wifi_clients_count !== undefined) {
      const elCount = document.getElementById('nav-wifi-count');
      if (elCount) {
        if (data.wifi_clients_count > 0) {
          elCount.innerText = data.wifi_clients_count;
          elCount.classList.remove('hidden');
        } else {
          elCount.classList.add('hidden');
        }
      }
    }

    if (data.uptime) document.getElementById('ov-uptime').innerText = '已连续运行: ' + formatUptime(data.uptime);
    if (manual) showToast('数据已更新');
  }
}

function updateThroughput(rxRate, txRate) {
  document.getElementById('ov-rx-speed').innerHTML = formatBytes(rxRate) + '/s';
  document.getElementById('ov-tx-speed').innerHTML = formatBytes(txRate) + '/s';
  const curRxEl = document.getElementById('chart-cur-rx');
  const curTxEl = document.getElementById('chart-cur-tx');
  if (curRxEl) curRxEl.innerText = formatBytes(rxRate) + '/s';
  if (curTxEl) curTxEl.innerText = formatBytes(txRate) + '/s';

  rxHistory.shift();
  rxHistory.push(rxRate);
  txHistory.shift();
  txHistory.push(txRate);
}

let isPollingRealtimeNet = false;
async function pollRealtimeNet() {
  if (!isAuthed || document.hidden || isPollingRealtimeNet) return;
  const ovPanel = document.getElementById('panel-overview');
  if (ovPanel && ovPanel.classList.contains('hidden')) return;
  isPollingRealtimeNet = true;
  try {
    const resp = await fetch('/api/realtime_net');
    if (resp.ok) {
      const res = await resp.json();
      if (res.ok) {
        updateThroughput(res.rx_rate || 0, res.tx_rate || 0);
      }
    }
  } catch (e) {
  } finally {
    isPollingRealtimeNet = false;
  }
}

async function dropSystemCaches() {
  showToast('正在释放 Linux 内核 PageCache 缓存...');
  const res = await apiCall('system.drop_caches');
  if (res && res.ok) {
    showToast(res.msg || '内核缓存已释放');
    fetchOverview(false);
  } else {
    showToast('释放失败: ' + ((res && res.error) || ''), false);
  }
}

const canvas = document.getElementById('trafficCanvas');
const ctx = canvas ? canvas.getContext('2d') : null;
let displayedScale = 8 * 1024; // 动态平滑量程基准
let _lastCanvasW = 0, _lastCanvasH = 0, _lastCanvasDpr = 0;

function renderTrafficChart() {
  if (!isAuthed || document.hidden || !ctx || !canvas) return;
  const ovPanel = document.getElementById('panel-overview');
  if (ovPanel && ovPanel.classList.contains('hidden')) return;
  const w = canvas.parentElement.clientWidth;
  const h = canvas.parentElement.clientHeight;
  if (!w || !h) return;
  const dpr = window.devicePixelRatio || 1;
  if (w !== _lastCanvasW || h !== _lastCanvasH || dpr !== _lastCanvasDpr) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    _lastCanvasW = w;
    _lastCanvasH = h;
    _lastCanvasDpr = dpr;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  // 自适应量程计算：以窗口内最大吞吐为准，辅以 1.35 倍裕度，最低保底 3.5KB/s（即使仅 1KB/s 也能充满 40% 屏幕高度）
  const currentPeak = Math.max(...rxHistory, ...txHistory, 500);
  const targetScale = Math.max(3.5 * 1024, currentPeak * 1.35);
  displayedScale += (targetScale - displayedScale) * 0.18; // 柔和插值

  const badge = document.getElementById('chart-scale-badge');
  if (badge) {
    badge.innerText = `量程: 0 ~ ${formatBytes(displayedScale)}/s (自适应)`;
  }

  // 绘制 3 条水平参考虚线与右侧标尺数值
  const levels = [
    { pct: 0.15, label: formatBytes(displayedScale) + '/s' },
    { pct: 0.50, label: formatBytes(displayedScale * 0.5) + '/s' },
    { pct: 0.80, label: formatBytes(displayedScale * 0.2) + '/s' }
  ];

  levels.forEach(lvl => {
    const y = h * lvl.pct;
    ctx.beginPath();
    ctx.strokeStyle = "rgba(255, 255, 255, 0.08)";
    ctx.setLineDash([4, 6]);
    ctx.lineWidth = 1;
    ctx.moveTo(0, y);
    ctx.lineTo(w, y);
    ctx.stroke();
    ctx.setLineDash([]);

    // 右侧标尺数值标签
    ctx.font = "10px monospace";
    ctx.fillStyle = "rgba(148, 163, 184, 0.8)";
    ctx.textAlign = "right";
    ctx.fillText(lvl.label, w - 8, y - 4);
  });

  // 底部 0 刻度基线
  ctx.beginPath();
  ctx.strokeStyle = "rgba(255, 255, 255, 0.18)";
  ctx.lineWidth = 1.5;
  ctx.moveTo(0, h - 6);
  ctx.lineTo(w, h - 6);
  ctx.stroke();

  // 平滑贝塞尔曲线与区域霓虹发光绘制函数
  function drawSpline(data, strokeColor, fillColor, glowColor) {
    ctx.save();
    const step = w / (data.length - 1);
    const points = data.map((v, i) => ({
      x: i * step,
      y: Math.max(10, Math.min(h - 6, h - (v / displayedScale) * (h - 26) - 6))
    }));

    // 1. 区域平滑渐变填充
    ctx.beginPath();
    ctx.moveTo(points[0].x, points[0].y);
    for (let i = 0; i < points.length - 1; i++) {
      const p0 = points[i];
      const p1 = points[i + 1];
      const cpx = (p0.x + p1.x) / 2;
      ctx.bezierCurveTo(cpx, p0.y, cpx, p1.y, p1.x, p1.y);
    }
    ctx.lineTo(w, h - 6);
    ctx.lineTo(0, h - 6);
    ctx.closePath();

    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, fillColor);
    grad.addColorStop(1, "rgba(0, 0, 0, 0)");
    ctx.fillStyle = grad;
    ctx.fill();

    // 2. 霓虹发光线条
    ctx.beginPath();
    ctx.moveTo(points[0].x, points[0].y);
    for (let i = 0; i < points.length - 1; i++) {
      const p0 = points[i];
      const p1 = points[i + 1];
      const cpx = (p0.x + p1.x) / 2;
      ctx.bezierCurveTo(cpx, p0.y, cpx, p1.y, p1.x, p1.y);
    }
    ctx.strokeStyle = glowColor;
    ctx.globalAlpha = 0.22;
    ctx.lineWidth = 7;
    ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.strokeStyle = strokeColor;
    ctx.lineWidth = 2.5;
    ctx.stroke();

    // 3. 最新数据点（右侧端点）绘制轻量双环高亮（零 shadowBlur 卷积开销）
    const lastPt = points[points.length - 1];
    ctx.beginPath();
    ctx.arc(lastPt.x - 2, lastPt.y, 7, 0, Math.PI * 2);
    ctx.fillStyle = fillColor;
    ctx.fill();
    ctx.beginPath();
    ctx.arc(lastPt.x - 2, lastPt.y, 3.5, 0, Math.PI * 2);
    ctx.fillStyle = strokeColor;
    ctx.fill();

    ctx.restore();
  }

  // 绘制下行（高亮青色）与上行（高亮紫色）
  drawSpline(rxHistory, "#22d3ee", "rgba(34, 211, 238, 0.35)", "rgba(34, 211, 238, 0.9)");
  drawSpline(txHistory, "#c084fc", "rgba(192, 132, 252, 0.25)", "rgba(192, 132, 252, 0.9)");
}

// =========================================================================
// VPN 全套逻辑实现
// =========================================================================
let currentVpnProfileId = 'mihomo';
let currentVpnNodesData = null;
let vpnEnabledState = false;
let vpnKillswitchState = false;

function switchVpnSubTab(subId) {
  document.querySelectorAll('.vpn-sub-panel').forEach(p => p.classList.add('hidden'));
  document.querySelectorAll('.vpn-subtab').forEach(b => {
    b.classList.remove('subtab-active');
    b.classList.add('bg-slate-900', 'text-slate-400');
  });

  const p = document.getElementById(`vpn-sub-${subId}`);
  if (p) p.classList.remove('hidden');

  const b = document.querySelector(`[data-sub="${subId}"]`);
  if (b) {
    b.classList.add('subtab-active');
    b.classList.remove('bg-slate-900', 'text-slate-400');
  }

  if (subId === 'subs') loadVpnSubs();
  if (subId === 'profiles') loadVpnProfiles();
  if (subId === 'engines') loadVpnEngines();
  if (subId === 'logs') fetchVpnLogs();
}

async function loadFullVpnData() {
  const res = await apiCall('vpn.status');
  if (res && res.status) {
    const s = res.status;
    const enabled = !!s.enabled;
    const running = !!s.running;
    vpnEnabledState = enabled;

    document.getElementById('vpn-active-badge').innerText = running ? '已连接 · 运行中' : (enabled ? '正在连接...' : '服务已停止');
    document.getElementById('vpn-active-badge').className = running
      ? 'px-2 py-0.5 rounded text-[11px] font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
      : 'px-2 py-0.5 rounded text-[11px] font-bold bg-slate-800 text-slate-400 border border-slate-700';

    const toggleBtn = document.getElementById('btn-vpn-main-toggle');
    toggleBtn.innerText = enabled ? '停止 VPN' : '启动 VPN';
    toggleBtn.className = enabled
      ? 'px-3.5 py-1.5 bg-rose-500/10 hover:bg-rose-500/20 text-rose-400 border border-rose-500/20 rounded-xl text-xs font-semibold transition'
      : 'px-3.5 py-1.5 bg-emerald-500 hover:bg-emerald-400 text-black rounded-xl text-xs font-bold transition';

    const ks = s.kill_switch || {};
    vpnKillswitchState = !!ks.setting;
    document.getElementById('btn-vpn-ks').innerText = '断网保护: ' + (ks.setting ? '开启' : '关闭');

    if (s.profile) {
      currentVpnProfileId = s.profile.id;
      document.getElementById('vpn-cur-profile').innerText = (s.profile.name || s.profile.id) + ' (' + (s.profile.type || 'mihomo') + ')';
    }
    if (s.engine) {
      document.getElementById('vpn-engine-name').innerText = typeof s.engine === 'object' ? s.engine.name : s.engine;
    }
    if (s.tunnel) {
      document.getElementById('vpn-tunnel-status').innerText = (s.tunnel.iface || 'tun') + (s.tunnel.up ? ' (UP)' : ' (DOWN)');
    }
    if (s.exit_ip) {
      document.getElementById('vpn-exit-ip').innerText = s.exit_ip;
    }

    // 填充 Profiles 下拉框
    const profRes = await apiCall('vpn.profiles');
    if (profRes && profRes.profiles) {
      const select = document.getElementById('vpn-select-profile');
      select.innerHTML = profRes.profiles.map(p =>
        `<option value="${p.id}" ${p.id === currentVpnProfileId ? 'selected' : ''}>${p.name || p.id} (${p.type})</option>`
      ).join('');
    }

    // 加载节点
    loadVpnNodes(currentVpnProfileId);
  }
}

function syncGroupSelectOptions(groups) {
  const bindSel = document.getElementById('bind-dev-group');
  if (!bindSel || !groups || !groups.length) return;
  const curVal = bindSel.value;
  const opts = groups.map(g => `<option value="${escapeHtml(g.name)}">🚀 ${escapeHtml(g.name)} (${Math.max(0, (g.nodes || []).length - 1)} 节点)</option>`);
  opts.push('<option value="DIRECT">⚡ 纯直连 (DIRECT 不经过代理)</option>');
  bindSel.innerHTML = opts.join('');
  if (curVal && (curVal === 'DIRECT' || groups.some(g => g.name === curVal))) {
    bindSel.value = curVal;
  }
}

async function loadVpnNodes(profileId) {
  const tbody = document.getElementById('vpn-nodes-tbody');
  const groupSelect = document.getElementById('vpn-select-group');
  const prevGroup = groupSelect ? groupSelect.value : '';
  tbody.innerHTML = '<tr><td colspan="4" class="py-6 text-center text-slate-500 font-sans">正在加载代理节点与策略组...</td></tr>';

  const res = await apiCall('vpn.nodes', { id: profileId || 'mihomo' });
  if (res && res.ok && res.groups && res.groups.length) {
    currentVpnNodesData = res;
    if (groupSelect) {
      groupSelect.innerHTML = res.groups.map(g => {
        return `<option value="${escapeHtml(g.name)}">🚀 ${escapeHtml(g.name)} (${g.nodes.length}个节点)</option>`;
      }).join('');
      if (prevGroup && res.groups.some(g => g.name === prevGroup)) {
        groupSelect.value = prevGroup;
      }
    }
    syncGroupSelectOptions(res.groups);
    updateDualGroupSummary(res);
    renderNodesForCurrentGroup();
  } else {
    tbody.innerHTML = `<tr><td colspan="4" class="py-6 text-center text-slate-400 font-sans">${(res && (res.error || res.detail)) || '暂无可用节点，请先在「订阅管理」中添加订阅链接'}</td></tr>`;
  }
}

function updateDualGroupSummary(res) {
  const grid = document.getElementById('vpn-groups-summary-grid');
  if (!grid || !res || !res.groups) return;
  const groups = res.groups || [];
  const policies = res.policies || {};
  const defGrp = policies.default_group || (groups.length ? groups[groups.length - 1].name : 'PROXY');
  const clients = policies.clients || [];

  const groupCards = groups.slice(0, 2).map((g, idx) => {
    const isDefault = g.name === defGrp;
    const themeCls = idx === 0
      ? 'bg-indigo-500/10 border-indigo-500/30 hover:border-indigo-400 text-indigo-300'
      : 'bg-cyan-500/10 border-cyan-500/30 hover:border-cyan-400 text-cyan-300';
    const badgeCls = idx === 0 ? 'bg-indigo-500/20 text-indigo-300' : 'bg-cyan-500/20 text-cyan-300';
    const nodeCnt = Math.max(0, (g.nodes || []).length - 1);
    return `
      <div onclick="quickSwitchVpnGroup('${escapeAttr(g.name)}')" class="p-3.5 rounded-2xl border cursor-pointer transition ${themeCls}">
        <div class="flex items-center justify-between">
          <span class="font-bold">🚀 ${escapeHtml(g.name)} ${isDefault ? '<span class="text-[10px] opacity-80">(默认出口)</span>' : ''}</span>
          <span class="text-[10px] px-2 py-0.5 rounded font-mono ${badgeCls}">${nodeCnt} 节点</span>
        </div>
        <div class="mt-1.5 font-mono text-white font-semibold truncate">当前: ${escapeHtml(g.selected || '--')}</div>
        <div class="mt-1 text-[11px] font-mono text-emerald-400 truncate" id="vpn-grp-exit-${idx}">出口校验: 点击右上角「出口探测」</div>
        <div class="mt-0.5 text-[11px] text-slate-400">${isDefault ? '未单独绑定的局域网终端默认走此策略组' : '可在设备管理中将指定终端 IP 分流至此策略组'}</div>
      </div>
    `;
  });

  const policyCard = `
    <div class="p-3.5 rounded-2xl bg-slate-900/70 border border-slate-800 flex flex-col justify-between">
      <div>
        <div class="font-bold text-white flex items-center justify-between">
          <span>📌 终端 IP 分流规则</span>
          <span class="text-[11px] text-emerald-400 font-mono" id="vpn-bound-summary">共 ${clients.length} 条设备绑定</span>
        </div>
        <p class="text-[11px] text-slate-400 mt-1">支持按 MAC 分配固定 IP 并指定走任意策略组或纯直连 (DIRECT)</p>
      </div>
      <button onclick="jumpToLanPolicyCard()" class="mt-2 w-full py-1.5 bg-slate-800 hover:bg-slate-700 text-cyan-300 rounded-xl text-xs font-medium border border-slate-700 transition">
        管理设备固定 IP 与策略组分流 &rarr;
      </button>
    </div>
  `;

  grid.innerHTML = groupCards.join('') + policyCard;
}

function quickSwitchVpnGroup(groupName) {
  const groupSelect = document.getElementById('vpn-select-group');
  if (groupSelect) {
    groupSelect.value = groupName;
    renderNodesForCurrentGroup();
  }
}

function jumpToLanPolicyCard() {
  switchTab('wifi');
  setTimeout(() => {
    const card = document.getElementById('lan-policy-card');
    if (card) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, 150);
}

function renderNodesForCurrentGroup() {
  if (!currentVpnNodesData || !currentVpnNodesData.groups) return;
  const groupName = document.getElementById('vpn-select-group').value;
  const g = currentVpnNodesData.groups.find(x => x.name === groupName) || currentVpnNodesData.groups[0];
  const tbody = document.getElementById('vpn-nodes-tbody');
  const selectedNode = g.selected;

  tbody.innerHTML = g.nodes.map(n => {
    const isSel = n.name === selectedNode;
    const rowBg = isSel ? 'bg-cyan-500/10' : '';
    const delayTxt = n.delay ? `${n.delay} ms` : (n.tested ? '超时' : '未测');
    const delayColor = n.delay ? (n.delay < 150 ? 'text-emerald-400' : 'text-amber-400') : 'text-slate-500';

    return `
      <tr class="${rowBg} hover:bg-slate-800/40 transition">
        <td class="py-3 pl-2 font-medium ${isSel ? 'text-cyan-300 font-bold' : 'text-white'}">
          ${isSel ? '<span class="inline-block w-2 h-2 rounded-full bg-cyan-400 mr-1.5 animate-pulse"></span>' : ''}
          ${n.name}
        </td>
        <td class="py-3 text-slate-400">${n.type}</td>
        <td class="py-3 font-mono ${delayColor}" id="delay-${safeId(n.name)}">${delayTxt}</td>
        <td class="py-3 pr-2 text-right space-x-1.5 font-sans">
          <button onclick="testSingleNodeDelay('${g.name}', '${escapeAttr(n.name)}')" class="px-2.5 py-1 bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white rounded-lg text-xs transition">测速</button>
          <button onclick="selectVpnNode('${g.name}', '${escapeAttr(n.name)}')" class="px-2.5 py-1 ${isSel ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30' : 'bg-cyan-600 hover:bg-cyan-500 text-black font-semibold'} rounded-lg text-xs transition" ${isSel ? 'disabled' : ''}>
            ${isSel ? '当前选中' : '选择'}
          </button>
        </td>
      </tr>
    `;
  }).join('');
}

function safeId(str) {
  return btoa(unescape(encodeURIComponent(str))).replace(/[^a-zA-Z0-9]/g, '');
}

function escapeAttr(str) {
  return str.replace(/'/g, "\\'");
}

async function testSingleNodeDelay(groupName, nodeName) {
  const cell = document.getElementById(`delay-${safeId(nodeName)}`);
  if (cell) cell.innerText = '测速中...';
  const res = await apiCall('vpn.node_delay', { id: currentVpnProfileId, group: groupName, node: nodeName });
  if (cell) {
    if (res && res.delay > 0) {
      cell.innerText = `${res.delay} ms`;
      cell.className = 'py-3 font-mono ' + (res.delay < 150 ? 'text-emerald-400' : 'text-amber-400');
    } else {
      cell.innerText = '超时/失败';
      cell.className = 'py-3 font-mono text-rose-400';
    }
  }
}

async function testAllNodesDelay() {
  if (!currentVpnNodesData) return;
  const groupName = document.getElementById('vpn-select-group').value;
  const g = currentVpnNodesData.groups.find(x => x.name === groupName);
  if (!g || !g.nodes.length) return;

  const btn = document.getElementById('btn-test-all-nodes');
  btn.disabled = true;
  btn.innerText = '正在并发测速...';

  const nodes = g.nodes;
  let idx = 0;
  async function worker() {
    while (idx < nodes.length) {
      const n = nodes[idx++];
      await testSingleNodeDelay(groupName, n.name);
    }
  }

  await Promise.all([worker(), worker(), worker()]);
  btn.disabled = false;
  btn.innerText = '一键测速当前组';
  showToast(`【${groupName}】全部节点测速完毕`);
}

async function selectVpnNode(groupName, nodeName) {
  showToast(`正在将【${groupName}】切换至: ${nodeName}...`);
  const res = await apiCall('vpn.node_select', { id: currentVpnProfileId, group: groupName, node: nodeName });
  if (res && res.ok) {
    showToast(`【${groupName}】已无感切换至: ${nodeName}`);
    currentVpnNodesData = res;
    updateDualGroupSummary(res);
    renderNodesForCurrentGroup();
  } else {
    showToast('切换失败: ' + (res.error || '未知错误'), false);
  }
}

async function changeVpnProfile(profId) {
  showToast(`正在切换到配置: ${profId}...`);
  const res = await apiCall('vpn.use', { id: profId });
  if (res && res.ok) {
    showToast('配置切换成功');
    loadFullVpnData();
  } else {
    showToast('切换失败: ' + (res.error || '错误'), false);
  }
}

async function fetchExitIp() {
  const el = document.getElementById('vpn-exit-ip');
  const el0 = document.getElementById('vpn-grp-exit-0');
  const el1 = document.getElementById('vpn-grp-exit-1');
  const btn = document.getElementById('btn-probe-ip');
  if (el) el.innerText = '正在探测出口 IP...';
  if (el0) el0.innerText = '正在探测策略组真实出口 IP 与 ISP...';
  if (el1) el1.innerText = '正在探测策略组真实出口 IP 与 ISP...';
  if (btn) btn.disabled = true;

  try {
    const res = await apiCall('vpn.group_exit_ips');
    if (res && res.ok) {
      const list = res.groups || [res.home, res.direct].filter(Boolean);
      list.forEach((item, idx) => {
        const targetEl = document.getElementById(`vpn-grp-exit-${idx}`);
        if (!targetEl) return;
        if (item && item.ip) {
          const loc = [item.country, item.region].filter(Boolean).join('·');
          const ispTag = item.is_residential ? '🏡非机房/住宅ISP' : (item.hosting === true ? '🏢数据中心' : '🌐ISP');
          targetEl.innerText = `出口: ${item.ip} (${loc || '海外'} · ${item.isp || item.org || 'ISP'} · ${ispTag})`;
        } else {
          targetEl.innerText = '出口: 探测超时 (请切换该组节点后再试)';
        }
      });

      if (el) {
        const summaryParts = list.filter(Boolean).map(x => `${x.group}:${x.ip || '超时'}`);
        el.innerText = summaryParts.length ? summaryParts.join(' | ') : '探测超时';
      }
      showToast('策略组出口 IP 探测完成');
      return;
    }
  } catch (e) {} finally {
    if (btn) btn.disabled = false;
  }

  if (el) el.innerText = '未获取到出口 IP';
}

async function toggleVpnService(op) {
  let realOp = op;
  if (op === 'toggle') {
    realOp = vpnEnabledState ? 'off' : 'on';
  }
  showToast(`正在下发 VPN 指令: ${realOp}...`);
  const res = await apiCall('vpn.service', { op: realOp });
  if (res && res.ok) {
    showToast('操作成功');
    setTimeout(loadFullVpnData, 1500);
  } else {
    showToast('操作失败: ' + (res.error || ''), false);
  }
}

async function toggleVpnKillswitch() {
  const nextVal = vpnKillswitchState ? 'off' : 'on';
  const res = await apiCall('vpn.killswitch', { value: nextVal });
  if (res && res.ok) {
    showToast(`断网保护已${nextVal === 'on' ? '开启' : '关闭'}`);
  } else {
    showToast('切换断网保护失败: ' + (res.error || ''), false);
  }
  loadFullVpnData();
}

// 订阅管理
async function loadVpnSubs() {
  const box = document.getElementById('vpn-subs-list');
  box.innerHTML = '<div class="text-slate-500 text-center py-6">正在加载订阅源...</div>';
  const res = await apiCall('vpn.subs');
  if (res && res.subscriptions && res.subscriptions.length) {
    box.innerHTML = res.subscriptions.map(s => `
      <div class="p-3 bg-slate-900/60 border border-slate-800 rounded-xl flex items-center justify-between">
        <div>
          <div class="flex items-center gap-2">
            <span class="font-bold text-white text-xs">${escapeHtml(s.name || s.id)}</span>
            <span class="px-2 py-0.5 rounded bg-indigo-500/20 text-indigo-300 border border-indigo-500/30 text-[10px] font-mono">策略组: ${escapeHtml(s.group || s.name || s.id)}</span>
          </div>
          <div class="text-[11px] text-slate-400 font-mono mt-0.5 truncate max-w-xs sm:max-w-md">${escapeHtml(s.host || '')} · 包含 ${s.count || 0} 个节点</div>
        </div>
        <div class="flex items-center gap-2">
          <button onclick="refreshSingleSub('${escapeAttr(s.id)}')" class="px-2.5 py-1 bg-cyan-600 hover:bg-cyan-500 text-black font-bold text-xs rounded-lg transition">更新</button>
          <button onclick="deleteSingleSub('${escapeAttr(s.id)}')" class="px-2.5 py-1 bg-slate-800 hover:bg-rose-950 text-rose-400 rounded-lg text-xs transition">删除</button>
        </div>
      </div>
    `).join('');
  } else {
    box.innerHTML = '<div class="text-slate-500 text-center py-6">暂无订阅，请在右侧添加 Clash / Mihomo 订阅链接</div>';
  }
}

async function refreshAllSubs() {
  showToast('正在更新全部订阅源并重新合成配置...');
  await apiCall('vpn.sub_refresh', { id: 'all' });
  showToast('所有订阅已更新并生效');
  loadVpnSubs();
  loadVpnNodes(currentVpnProfileId);
}

async function refreshSingleSub(id) {
  showToast(`正在刷新订阅 ${id}...`);
  await apiCall('vpn.sub_refresh', { id });
  showToast('更新完成');
  loadVpnSubs();
  loadVpnNodes(currentVpnProfileId);
}

async function deleteSingleSub(id) {
  if (!confirm(`确定删除订阅 ${id} 吗？`)) return;
  await apiCall('vpn.sub_remove', { id });
  showToast('订阅已删除');
  loadVpnSubs();
  loadVpnNodes(currentVpnProfileId);
}

async function addSubscription() {
  const name = document.getElementById('sub-add-name').value.trim();
  const groupEl = document.getElementById('sub-add-group');
  const group = groupEl ? groupEl.value.trim() : '';
  const url = document.getElementById('sub-add-url').value.trim();
  const interval = Number(document.getElementById('sub-add-int').value) || 24;
  if (!name || !url) return showToast('请输入订阅名称和 URL', false);
  showToast('正在添加并拉取订阅节点...');
  const res = await apiCall('vpn.sub_add', { name, group: group || name, url, interval });
  if (res && res.ok) {
    showToast('订阅添加成功并已自动生成策略组');
    document.getElementById('sub-add-url').value = '';
    loadVpnSubs();
    loadVpnNodes(currentVpnProfileId);
  } else {
    showToast('添加失败: ' + (res.error || ''), false);
  }
}

// Profiles 管理
async function loadVpnProfiles() {
  const box = document.getElementById('vpn-profiles-list');
  box.innerHTML = '<div class="text-slate-500 text-center py-6">正在读取配置文件...</div>';
  const res = await apiCall('vpn.profiles');
  if (res && res.profiles && res.profiles.length) {
    box.innerHTML = res.profiles.map(p => `
      <div class="p-3 bg-slate-900/60 border border-slate-800 rounded-xl flex items-center justify-between">
        <div>
          <div class="font-bold text-white text-xs">${p.name || p.id} ${p.active ? '<span class="px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-400 text-[10px] ml-1">当前在用</span>' : ''}</div>
          <div class="text-[11px] text-slate-400 font-mono mt-0.5">类型: ${p.type} · 核心: ${p.engine}</div>
        </div>
        <div class="flex items-center gap-2">
          ${!p.active ? `<button onclick="changeVpnProfile('${p.id}')" class="px-2.5 py-1 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg text-xs">启用</button>` : ''}
          <button onclick="removeVpnProfile('${p.id}')" class="px-2 py-1 text-rose-400 hover:underline text-xs">删除</button>
        </div>
      </div>
    `).join('');
  } else {
    box.innerHTML = '<div class="text-slate-500 text-center py-6">暂无配置文件</div>';
  }
}

async function addProfileDirect() {
  const d = document.getElementById('profile-add-data').value.trim();
  const name = document.getElementById('profile-add-name').value.trim();
  if (!d) return showToast('请粘贴配置或节点分享链接', false);
  showToast('正在解析并导入配置...');
  const isLink = /^(vless|vmess|trojan|ss|hysteria2|hy2|tuic|wireguard|wg):\/\//.test(d);
  const req = { source: isLink ? 'link' : 'text', data: d };
  if (name) req.name = name;
  const res = await apiCall('vpn.add', req);
  if (res && res.ok) {
    showToast('导入成功');
    document.getElementById('profile-add-data').value = '';
    loadVpnProfiles();
  } else {
    showToast('导入失败: ' + (res.error || ''), false);
  }
}

async function removeVpnProfile(id) {
  if (!confirm(`确定删除配置 ${id} 吗？`)) return;
  await apiCall('vpn.remove', { id });
  showToast('配置已删除');
  loadVpnProfiles();
}

// 核心引擎
async function loadVpnEngines() {
  const tbody = document.getElementById('vpn-engines-tbody');
  tbody.innerHTML = '<tr><td colspan="5" class="py-6 text-center text-slate-500 font-sans">正在读取核心引擎状态...</td></tr>';
  const res = await apiCall('vpn.engines');
  if (res && res.engines) {
    tbody.innerHTML = res.engines.map(e => `
      <tr class="hover:bg-slate-800/40 transition">
        <td class="py-3 font-bold text-white font-sans">${e.name}</td>
        <td class="py-3">${e.installed ? '<span class="text-emerald-400">已就绪</span>' : '<span class="text-slate-500">未安装</span>'}</td>
        <td class="py-3 text-slate-400">${e.version || '--'}</td>
        <td class="py-3 text-slate-400">${e.size ? formatBytes(e.size) : '--'}</td>
        <td class="py-3 text-right">
          ${!e.installed ? `<button onclick="installVpnEngine('${e.name}')" class="px-2.5 py-1 bg-cyan-600 hover:bg-cyan-500 text-black font-bold rounded-lg text-xs">一键安装</button>` : '<span class="text-xs text-slate-500">最新版</span>'}
        </td>
      </tr>
    `).join('');
  }
}

async function installVpnEngine(name) {
  showToast(`正在下载并安装引擎: ${name} (需要网络)...`);
  await apiCall('vpn.engine_install', { name });
  showToast('安装任务已下发');
  setTimeout(loadVpnEngines, 4000);
}

// VPN 日志
async function fetchVpnLogs() {
  const box = document.getElementById('vpn-log-content');
  box.innerText = '正在读取日志...';
  const res = await apiCall('vpn.logs', { lines: 200 });
  if (res && res.log) {
    box.innerText = res.log;
    box.scrollTop = box.scrollHeight;
  } else {
    box.innerText = '暂无运行日志';
  }
}

// =========================================================================
// 短信、OTP 验证码提取、Bark/TG/Webhook 推送、USSD 与信令语音保号
// =========================================================================
function getSelectedSmsSlot() {
  const el = document.getElementById('sms-slot-select');
  if (!el || !el.value) return null;
  const n = parseInt(el.value, 10);
  return (n === 1 || n === 2) ? n : null;
}

function onSmsSlotChange() {
  const sl = getSelectedSmsSlot();
  const hint = document.getElementById('sms-send-slot-hint');
  if (hint) hint.innerText = sl ? `使用: SIM ${sl}` : '使用: 当前主卡';
  fetchSmsList();
  loadCallStatus();
}

async function loadSmsTab() {
  fetchSmsList();
  loadCommPushConfig();
  loadCallStatus();
}

async function fetchSmsList() {
  const container = document.getElementById('sms-list-container');
  if (container) container.innerHTML = '<div class="text-center py-6 text-xs text-slate-500">正在读取短信...</div>';
  const sl = getSelectedSmsSlot();
  const req = sl ? { slot: sl } : {};
  const res = await apiCall('sms.list', req);
  const messages = (res && res.messages) || (res && res.data && res.data.messages) || [];
  const badge = document.getElementById('sms-count-badge');
  if (badge) badge.innerText = `${messages.length} 条 · SIM ${res && res.slot ? res.slot : (sl || '主卡')}`;
  if (!container) return;

  if (messages.length) {
    container.innerHTML = messages.map(m => {
      const textBody = m.text || m.body || '';
      const sender = m.sender || m.number || m.from || '未知号码';
      const dateStr = m.time || m.date || m.timestamp || '';
      const msgId = m.id !== undefined ? m.id : m.index;
      const otp = m.otp_code || ((textBody.match(/\b\d{4,8}\b/) || [])[0] || '');
      const codeHtml = otp
        ? `<button onclick="copyText('${escapeJs(otp)}')" class="px-2 py-0.5 rounded-lg bg-cyan-500/20 text-cyan-200 border border-cyan-500/40 text-[10px] font-mono font-bold hover:bg-cyan-500/35 ml-2 transition">🔑 复制验证码: ${escapeHtml(otp)}</button>`
        : '';
      return `
        <div class="p-3 bg-slate-900/75 border border-slate-800 hover:border-slate-700 rounded-xl space-y-1.5 text-xs transition">
          <div class="flex flex-wrap justify-between items-center gap-1 text-slate-400">
            <span class="font-bold text-white font-mono flex items-center flex-wrap">${escapeHtml(sender)}${codeHtml}</span>
            <span class="text-[11px] font-mono text-slate-400">${escapeHtml(dateStr)}</span>
          </div>
          <div class="text-slate-200 leading-relaxed break-words">${escapeHtml(textBody)}</div>
          <div class="flex justify-end pt-1">
            <button onclick="deleteSms('${msgId}')" class="text-[10px] text-rose-400 hover:text-rose-300">删除短信</button>
          </div>
        </div>
      `;
    }).join('');
  } else {
    const errHint = (res && !res.ok && res.error) ? `（提示: ${escapeHtml(res.error)}）` : '';
    container.innerHTML = `<div class="text-center py-8 text-xs text-slate-500">当前所选卡槽收件箱暂无短信 ${errHint}</div>`;
  }
}

function copyText(txt, label = '') {
  if (!txt) return;
  const str = String(txt);
  const disp = str.length > 14 ? `${str.slice(0, 6)}••••••${str.slice(-4)} (完整 ${str.length} 位)` : str;
  const msg = label ? `已复制${label}: ${disp}` : `已复制: ${disp}`;
  if (navigator.clipboard && window.isSecureContext && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(str).then(() => showToast(msg)).catch(() => {
      if (_legacyExecCopy(str)) showToast(msg);
      else window.prompt('请手动复制:', str);
    });
  } else if (_legacyExecCopy(str)) {
    showToast(msg);
  } else {
    window.prompt('请手动复制:', str);
  }
}

async function sendSms() {
  const to = document.getElementById('sms-to').value.trim();
  const content = document.getElementById('sms-content').value.trim();
  if (!to || !content) return showToast('请输入收件人号码和短信内容', false);
  const sl = getSelectedSmsSlot();
  showToast(`正在通过 ${sl ? ('SIM ' + sl) : '当前主卡'} 发送短信...`);
  const req = { number: to, text: content };
  if (sl) req.slot = sl;
  const res = await apiCall('sms.send', req);
  if (res && res.ok) {
    showToast('短信已发送成功');
    document.getElementById('sms-content').value = '';
  } else {
    showToast('发送失败: ' + ((res && res.error) || '请确认该卡已驻网或处于活动态'), false);
  }
}

async function deleteSms(id) {
  if (!confirm('确定删除此短信？')) return;
  const sl = getSelectedSmsSlot();
  const req = { id: String(id) };
  if (sl) req.slot = sl;
  await apiCall('sms.delete', req);
  showToast('短信已删除');
  fetchSmsList();
}

async function deleteAllSms() {
  const sl = getSelectedSmsSlot();
  if (!confirm(`确定清空 ${sl ? ('SIM ' + sl) : '当前主卡'} 收件箱中的全部短信吗？`)) return;
  const req = { id: 'all' };
  if (sl) req.slot = sl;
  const res = await apiCall('sms.delete', req);
  if (res && res.ok) {
    showToast('已清空该卡槽短信');
    fetchSmsList();
  } else {
    showToast('清空失败: ' + ((res && res.error) || ''), false);
  }
}

async function loadCommPushConfig() {
  const res = await apiCall('comm.push_get');
  if (!res || !res.ok) return;
  const cfg = res.config || {};
  if (document.getElementById('comm-push-enabled')) document.getElementById('comm-push-enabled').checked = !!cfg.enabled;
  if (document.getElementById('comm-push-sms')) document.getElementById('comm-push-sms').checked = cfg.push_sms !== false;
  if (document.getElementById('comm-push-calls')) document.getElementById('comm-push-calls').checked = cfg.push_calls !== false;
  if (document.getElementById('comm-auto-hangup')) document.getElementById('comm-auto-hangup').checked = !!cfg.auto_hangup_calls;

  const barkSt = document.getElementById('comm-bark-status');
  if (barkSt) {
    barkSt.innerText = cfg.bark_configured ? `已配置 (${cfg.bark_preview})` : '未配置';
    barkSt.className = cfg.bark_configured ? 'text-emerald-400 font-mono' : 'text-slate-500 font-mono';
  }
  const barkIn = document.getElementById('comm-bark-url');
  if (barkIn && !barkIn.value && cfg.bark_preview) {
    barkIn.placeholder = `已保存: ${cfg.bark_preview} (留空保持不变)`;
  }
  const tgTokIn = document.getElementById('comm-tg-token');
  if (tgTokIn && cfg.tg_configured) {
    tgTokIn.placeholder = `已配置 (${cfg.tg_token_preview})`;
  }
  const tgChatIn = document.getElementById('comm-tg-chat');
  if (tgChatIn && cfg.tg_chat_id) tgChatIn.value = cfg.tg_chat_id;

  const whIn = document.getElementById('comm-wh-url');
  if (whIn && !whIn.value && cfg.webhook_preview) {
    whIn.placeholder = `已保存: ${cfg.webhook_preview} (留空保持不变)`;
  }
  const whSecIn = document.getElementById('comm-wh-secret');
  if (whSecIn && cfg.webhook_secret_set) {
    whSecIn.placeholder = '已设置签名密钥';
  }

  const evBox = document.getElementById('comm-events-box');
  if (evBox) {
    const evs = res.events || [];
    if (!evs.length) {
      evBox.innerHTML = '<div class="text-slate-500">暂无推送或来电事件</div>';
    } else {
      evBox.innerHTML = evs.slice(0, 12).map(e => `
        <div class="p-1.5 rounded-lg bg-slate-900/70 border border-slate-800/80 flex flex-col gap-0.5">
          <div class="flex justify-between">
            <span class="${e.ok ? 'text-emerald-300' : 'text-rose-400'} font-semibold">${escapeHtml(e.title || '')}</span>
            <span class="text-slate-500 text-[10px]">${escapeHtml((e.time || '').slice(5))}</span>
          </div>
          <div class="text-slate-400 text-[10px] break-all">${escapeHtml(e.detail || '')}</div>
        </div>
      `).join('');
    }
  }
}

async function saveCommPushConfig() {
  const req = {
    enabled: document.getElementById('comm-push-enabled').checked,
    push_sms: document.getElementById('comm-push-sms').checked,
    push_calls: document.getElementById('comm-push-calls').checked,
    auto_hangup_calls: document.getElementById('comm-auto-hangup').checked,
    tg_chat_id: (document.getElementById('comm-tg-chat').value || '').trim(),
  };
  const barkVal = (document.getElementById('comm-bark-url').value || '').trim();
  if (barkVal) req.bark_url = barkVal;
  const tgTok = (document.getElementById('comm-tg-token').value || '').trim();
  if (tgTok) req.tg_bot_token = tgTok;
  const whUrl = (document.getElementById('comm-wh-url').value || '').trim();
  if (whUrl) req.webhook_url = whUrl;
  const whSec = (document.getElementById('comm-wh-secret').value || '').trim();
  if (whSec) req.webhook_secret = whSec;

  showToast('正在保存多通道推送与防打扰规则...');
  const res = await apiCall('comm.push_set', req);
  if (res && res.ok) {
    document.getElementById('comm-bark-url').value = '';
    document.getElementById('comm-tg-token').value = '';
    document.getElementById('comm-wh-url').value = '';
    document.getElementById('comm-wh-secret').value = '';
    showToast(res.msg || '推送与来电规则已保存');
    loadCommPushConfig();
  } else {
    showToast('保存失败: ' + ((res && res.error) || ''), false);
  }
}

async function testCommPush() {
  showToast('正在向已配置的推送通道发送验证码测试卡片...');
  const res = await apiCall('comm.push_test');
  if (res && res.ok) {
    showToast('测试推送已送达: ' + (res.summary || '成功'));
    loadCommPushConfig();
  } else {
    showToast('测试推送失败: ' + ((res && (res.error || res.summary)) || ''), false);
  }
}

function quickUssd(code) {
  const el = document.getElementById('ussd-code-input');
  if (el) el.value = code;
  sendUssdCode();
}

async function sendUssdCode() {
  const el = document.getElementById('ussd-code-input');
  const code = el ? el.value.trim() : '';
  if (!code) return showToast('请输入 USSD 指令（如 *101#）', false);
  const outBox = document.getElementById('ussd-output-box');
  const sl = getSelectedSmsSlot();
  if (outBox) outBox.innerText = `> 正在向 ${sl ? ('SIM ' + sl) : '当前主卡'} 发送 ${code} (等待基站响应，最长 45s)...`;
  const req = { code };
  if (sl) req.slot = sl;
  const res = await apiCall('ussd.send', req);
  if (res && res.ok) {
    if (outBox) outBox.innerText = res.text || '(运营商已受理，无文本返回)';
    showToast('USSD 响应已返回');
  } else {
    if (outBox) outBox.innerText = `ERROR: ${(res && res.error) || 'USSD 请求超时或该卡未驻网'}`;
    showToast('USSD 发送失败: ' + ((res && res.error) || ''), false);
  }
}

async function cancelUssdSession() {
  const sl = getSelectedSmsSlot();
  const req = sl ? { slot: sl } : {};
  await apiCall('ussd.cancel', req);
  const outBox = document.getElementById('ussd-output-box');
  if (outBox) outBox.innerText = '// USSD 交互会话已关闭';
  showToast('已结束 USSD 会话');
}

async function loadCallStatus() {
  const sl = getSelectedSmsSlot();
  const res = await apiCall('call.status', sl ? { slot: sl } : {});
  const box = document.getElementById('call-status-box');
  if (!box || !res || !res.ok) return;
  const calls = res.calls || [];
  const recents = res.recent_calls || [];
  let html = '';
  if (calls.length) {
    html += calls.map(c => `<div class="text-emerald-300 font-bold">🟢 [SIM ${res.slot}] ${escapeHtml(c.state_text)}: ${escapeHtml(c.number)}</div>`).join('');
  } else {
    html += `<div class="text-slate-400">SIM ${res.slot} 当前空闲（无活动语音呼叫）</div>`;
  }
  if (recents.length) {
    html += recents.slice(0, 4).map(r => `<div class="text-slate-500 text-[10px]">${escapeHtml((r.time || '').slice(5))} · ${escapeHtml(r.title || '')} (${escapeHtml(r.detail || '')})</div>`).join('');
  }
  box.innerHTML = html;
}

async function dialVoiceCall() {
  const num = (document.getElementById('call-dial-number').value || '').trim();
  if (!num) return showToast('请输入要呼叫的号码（如 10086）', false);
  const sl = getSelectedSmsSlot();
  showToast(`正在发起信令语音呼叫: ${num}...`);
  const req = { number: num };
  if (sl) req.slot = sl;
  const res = await apiCall('call.dial', req);
  if (res && res.ok) {
    showToast(res.msg || '呼叫已发起，可响铃数秒后点挂断完成保号');
    setTimeout(loadCallStatus, 1200);
  } else {
    showToast('呼叫失败: ' + ((res && res.error) || ''), false);
  }
}

async function sendCallDtmf() {
  const digits = (document.getElementById('call-dtmf-digits').value || '').trim();
  if (!digits) return;
  const sl = getSelectedSmsSlot();
  const req = { digits };
  if (sl) req.slot = sl;
  const res = await apiCall('call.dtmf', req);
  if (res && res.ok) {
    showToast(`已发送按键: ${res.digits}`);
    document.getElementById('call-dtmf-digits').value = '';
  } else {
    showToast('DTMF 发送失败: ' + ((res && res.error) || ''), false);
  }
}

async function hangupVoiceCall() {
  const sl = getSelectedSmsSlot();
  const res = await apiCall('call.hangup', sl ? { slot: sl } : {});
  showToast((res && res.msg) || '已发送挂断指令');
  setTimeout(loadCallStatus, 800);
}

// =========================================================================
// AT 终端 (支持切换 SIM 1 / SIM 2 独立 Broker 通道)
// =========================================================================
let atHistoryLoaded = false;
async function loadAtHistory() {
  if (atHistoryLoaded) return;
  atHistoryLoaded = true;
  const res = await apiCall('at.history');
  if (res && res.history && res.history.length) {
    const box = document.getElementById('at-console-out');
    res.history.slice(-10).forEach(h => {
      const item = document.createElement('div');
      item.className = 'text-slate-500 text-[11px] font-mono';
      item.innerText = `[历史 ${h.time || ''}] ${h.command}`;
      box.appendChild(item);
    });
    box.scrollTop = box.scrollHeight;
  }
}

function appendAtLog(cmd, out, slotTag = '') {
  const box = document.getElementById('at-console-out');
  const item = document.createElement('div');
  item.className = 'border-b border-slate-900 pb-1 mb-1';
  item.innerHTML = `<span class="text-cyan-400">&gt; ${slotTag ? '[' + escapeHtml(slotTag) + '] ' : ''}${escapeHtml(cmd)}</span><br><span class="text-emerald-300 whitespace-pre-wrap">${escapeHtml(out)}</span>`;
  box.appendChild(item);
  box.scrollTop = box.scrollHeight;
}

async function execAtCmd(cmd) {
  const slotEl = document.getElementById('at-slot-select');
  const sl = slotEl && slotEl.value ? parseInt(slotEl.value, 10) : null;
  const req = { command: cmd, timeout: 6 };
  if (sl === 1 || sl === 2) req.slot = sl;
  const res = await apiCall('at.send', req);
  const outText = res.text || res.response || (res.ok ? 'OK' : ('ERROR: ' + res.error));
  appendAtLog(cmd, outText, sl ? `SIM ${sl}` : '主卡');
}

function submitAtCmd() {
  const input = document.getElementById('at-cmd-input');
  const cmd = input.value.trim();
  if (!cmd) return;
  execAtCmd(cmd);
  input.value = '';
}

function clearAtTerminal() {
  document.getElementById('at-console-out').innerHTML = '<div class="text-slate-500">// 终端屏幕已清空</div>';
}

async function quickAction(type) {
  if (type === 'reboot') {
    if (!confirm('确定让 U30 Air 软重启吗？设备断网约 1~2 分钟。')) return;
    showToast('正在向设备下发 reboot 重启指令...');
    await apiCall('system.reboot');
  } else if (type === 'reconnect-data') {
    await fastRedialCellular();
  } else if (type === 'restart-modem') {
    showToast('正在重启调制解调器射频...');
    await apiCall('modem.restart');
  }
}

// =========================================================================
// 双卡双槽热切、改装无损触点检测、5 步健康诊断与分卡槽 APN 控制中心
// =========================================================================
let simOverviewCache = null;
let simSwitchPollTimer = null;
let apnSlotInitialized = false;
let slotProbeOutputs = {};

async function loadCellularHealth(manual = false) {
  if (manual) showToast('正在执行 5 步蜂窝链路端到端体检...');
  const res = await apiCall('cellular.health');
  if (!res || !res.ok) return;
  const badge = document.getElementById('net-health-overall');
  if (badge) {
    if (res.overall === 'healthy') {
      badge.innerText = `✅ ${res.score}/5 链路健康 (SIM ${res.slot} · ${res.iface})`;
      badge.className = 'px-2.5 py-0.5 rounded-full text-[11px] font-bold bg-emerald-500/15 text-emerald-300 border border-emerald-500/30 font-mono';
    } else if (res.overall === 'degraded') {
      badge.innerText = `⚠️ ${res.score}/5 部分受限 (SIM ${res.slot})`;
      badge.className = 'px-2.5 py-0.5 rounded-full text-[11px] font-bold bg-amber-500/15 text-amber-300 border border-amber-500/30 font-mono';
    } else {
      badge.innerText = `❌ ${res.score}/5 链路离线 (SIM ${res.slot})`;
      badge.className = 'px-2.5 py-0.5 rounded-full text-[11px] font-bold bg-rose-500/15 text-rose-300 border border-rose-500/30 font-mono';
    }
  }
  const box = document.getElementById('net-health-steps');
  if (box && Array.isArray(res.steps)) {
    box.innerHTML = res.steps.map(s => {
      const stCls = s.status === 'ok'
        ? 'border-emerald-500/30 bg-emerald-950/20 text-emerald-300'
        : (s.status === 'warn' ? 'border-amber-500/30 bg-amber-950/20 text-amber-300' : 'border-rose-500/35 bg-rose-950/25 text-rose-300');
      const dot = s.status === 'ok' ? '🟢' : (s.status === 'warn' ? '🟡' : '🔴');
      return `
        <div class="p-3 rounded-xl border ${stCls} space-y-1">
          <div class="font-bold text-white flex items-center justify-between text-xs">
            <span>${escapeHtml(s.title)}</span>
            <span>${dot}</span>
          </div>
          <div class="text-[11px] leading-snug opacity-95 font-mono break-all">${escapeHtml(s.summary || '')}</div>
        </div>
      `;
    }).join('');
  }
}

async function fastRedialCellular() {
  const btn = document.getElementById('btn-fast-redial');
  if (btn) { btn.disabled = true; btn.innerText = '⏳ 正在重拨换 IP...'; }
  showToast('正在执行秒级快速重拨，刷新运营商公网 IP...');
  try {
    const res = await apiCall('cellular.redial');
    if (res && res.ok) {
      const ipDiff = (res.old_ipv4 && res.new_ipv4 && res.old_ipv4 !== res.new_ipv4)
        ? ` (${res.old_ipv4} → ${res.new_ipv4})`
        : (res.new_ipv4 ? ` (当前 IP: ${res.new_ipv4})` : '');
      showToast(`快速重拨成功，耗时 ${res.elapsed_s}s${ipDiff}`);
      loadNetworkTab();
      fetchOverview(true);
    } else {
      showToast('快速重拨失败: ' + ((res && res.error) || ''), false);
    }
  } finally {
    if (btn) { btn.disabled = false; btn.innerText = '⚡ 秒换公网 IP (快速重拨)'; }
  }
}

async function toggleAirplaneMode() {
  const isOff = simOverviewCache && simOverviewCache.airplane_mode;
  const nextState = isOff ? 'on' : 'off';
  if (!isOff && !confirm('开启飞行模式将关闭双卡射频并断开蜂窝数据（仅保留 Wi-Fi 局域网管理），确认继续？')) return;
  showToast(isOff ? '正在关闭飞行模式并恢复基带射频...' : '正在开启飞行模式...');
  const res = await apiCall('radio.set', { slot: 'all', state: nextState });
  if (res && res.ok) {
    showToast(isOff ? '基带射频已恢复' : '已进入飞行模式');
    setTimeout(() => { loadSimOverview(false); loadCellularHealth(false); fetchOverview(true); }, 1800);
  } else {
    showToast('切换飞行模式失败: ' + ((res && res.error) || ''), false);
  }
}

async function loadSimOverview(refreshActive = false) {
  const res = await apiCall('sim.overview', { refresh: !!refreshActive });
  if (!res || !res.ok) return;
  simOverviewCache = res;

  const hotBadge = document.getElementById('sim-hot-badge');
  const btnHot = document.getElementById('btn-enable-sim-hot');
  const allBrokersUp = Array.isArray(res.slots) && res.slots.every(s => s.broker_up);
  if (hotBadge) {
    if (res.available && allBrokersUp) {
      hotBadge.innerText = `⚡ 双卡在线秒切已就绪 · 当前 SIM ${res.active_slot}`;
      hotBadge.className = 'px-2.5 py-0.5 rounded-full text-[11px] font-mono bg-emerald-500/15 text-emerald-300 border border-emerald-500/30';
      if (btnHot) btnHot.classList.add('hidden');
    } else {
      hotBadge.innerText = '⚠️ 副卡 Broker 未激活 (点击右侧一键激活)';
      hotBadge.className = 'px-2.5 py-0.5 rounded-full text-[11px] font-mono bg-amber-500/15 text-amber-300 border border-amber-500/30';
      if (btnHot) btnHot.classList.remove('hidden');
    }
  }

  const btnAir = document.getElementById('btn-airplane-mode');
  if (btnAir) {
    btnAir.innerText = res.airplane_mode ? '✈️ 飞行模式: 已开启 (点此恢复)' : '✈️ 飞行模式: 关';
    btnAir.className = res.airplane_mode
      ? 'px-3 py-1.5 rounded-xl bg-amber-500 text-black font-bold text-xs transition'
      : 'px-3 py-1.5 rounded-xl bg-slate-800 hover:bg-amber-500/20 text-slate-300 hover:text-amber-300 border border-slate-700 text-xs font-medium transition';
  }

  if (!apnSlotInitialized && document.getElementById('apn-target-slot')) {
    apnSlotInitialized = true;
    document.getElementById('apn-target-slot').value = String(res.active_slot || 1);
    loadSlotApnForm(res.active_slot || 1);
  }

  renderSimSwitchBanner(res.switch, res.flash);
  renderSimSlotCards(res);
  if ((res.switch && res.switch.running) || (res.flash && res.flash.running)) {
    startSimSwitchPolling();
  }
}

function renderSimSwitchBanner(sw, fl) {
  const banner = document.getElementById('sim-switch-banner');
  const titleEl = document.getElementById('sim-switch-title');
  const descEl = document.getElementById('sim-switch-desc');
  const cdEl = document.getElementById('sim-flash-countdown');
  const cancelBtn = document.getElementById('btn-cancel-flash');
  const spinEl = document.getElementById('sim-switch-spinner');
  if (!banner) return;

  if (fl && fl.running) {
    banner.classList.remove('hidden');
    banner.className = 'p-3.5 rounded-xl bg-amber-950/50 border border-amber-500/40 flex flex-col sm:flex-row sm:items-center justify-between gap-3 text-xs';
    if (spinEl) spinEl.className = 'w-2.5 h-2.5 rounded-full bg-amber-400 animate-ping shrink-0';
    if (titleEl) titleEl.innerText = `⏱️ 闪切接码运行中 (${fl.phase === 'listening' ? '正在副卡驻网监听短信' : '正在自动切回主卡'})`;
    if (descEl) descEl.innerText = fl.msg || '';
    if (cdEl) { cdEl.classList.remove('hidden'); cdEl.innerText = `${fl.remaining_s || 0}s`; }
    if (cancelBtn) cancelBtn.classList.remove('hidden');
  } else if (sw && sw.running) {
    banner.classList.remove('hidden');
    banner.className = 'p-3.5 rounded-xl bg-cyan-950/50 border border-cyan-500/40 flex flex-col sm:flex-row sm:items-center justify-between gap-3 text-xs';
    if (spinEl) spinEl.className = 'w-2.5 h-2.5 rounded-full bg-cyan-400 animate-ping shrink-0';
    if (titleEl) titleEl.innerText = `⚡ 正在热切换蜂窝主卡: SIM ${sw.from_slot} → SIM ${sw.target_slot} (已用时 ${sw.elapsed_s || 0}s)`;
    if (descEl) descEl.innerText = sw.msg || '目标卡若超时无法建立数据连接将自动安全回滚至原卡...';
    if (cdEl) cdEl.classList.add('hidden');
    if (cancelBtn) cancelBtn.classList.add('hidden');
  } else {
    banner.classList.add('hidden');
  }
}

function renderSimSlotCards(ov) {
  const grid = document.getElementById('sim-slots-grid');
  if (!grid || !Array.isArray(ov.slots)) return;

  grid.innerHTML = ov.slots.map(s => {
    const isAct = !!s.active;
    const prof = s.profile || {};
    const tr = s.traffic || {};
    const cardBorder = isAct
      ? 'border-emerald-500/50 bg-gradient-to-br from-slate-900/95 to-emerald-950/20 shadow-lg shadow-emerald-950/20'
      : 'border-slate-800 bg-slate-900/75 hover:border-slate-700';

    const statusBadge = isAct
      ? '<span class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-emerald-500 text-black">🟢 当前上网主卡</span>'
      : '<span class="px-2 py-0.5 rounded-full text-[10px] font-medium bg-slate-800 text-slate-300 border border-slate-700">⚪ 待机副卡</span>';
    const bootBadge = s.boot_default
      ? '<span class="px-2 py-0.5 rounded text-[10px] font-mono bg-cyan-500/15 text-cyan-300 border border-cyan-500/30">开机默认</span>'
      : '';
    const euiccBadge = s.is_euicc
      ? '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-purple-500/20 text-purple-300 border border-purple-500/40">✨ eUICC (9eSIM/ESTK 就绪)</span>'
      : '';
    const cpinCls = s.cpin === 'READY'
      ? 'text-emerald-300 bg-emerald-500/10 border-emerald-500/30'
      : (s.cpin === 'UNPROBED' ? 'text-slate-400 bg-slate-800 border-slate-700' : 'text-rose-300 bg-rose-500/15 border-rose-500/30');
    const cpinLabel = s.cpin === 'READY' ? '触点正常 · READY' : (s.cpin === 'UNPROBED' ? '待点触点检测' : `卡状态: ${s.cpin}`);

    const titleName = prof.nickname || s.label;
    const carrierStr = s.carrier || '待识别运营商';
    const capBytes = Number(tr.cap_bytes || 0);
    const usedBytes = Number(tr.cycle_bytes || 0);
    const usedPct = capBytes > 0 ? Math.min(100, Number(tr.used_pct || 0)) : 0;
    const overAct = prof.over_limit_action || 'none';
    const overTag = overAct === 'switch_sim'
      ? '<span class="text-[10px] text-amber-300 font-mono">⚡超额自动切副卡</span>'
      : (overAct === 'cut_data' ? '<span class="text-[10px] text-rose-300 font-mono">🛑超额断网</span>' : '');

    const probeHtml = slotProbeOutputs[s.slot] || '';

    return `
      <div class="p-4 rounded-2xl border ${cardBorder} space-y-3.5 text-xs transition flex flex-col justify-between">
        <div class="space-y-3">
          <div class="flex flex-wrap items-center justify-between gap-1.5">
            <div class="flex flex-wrap items-center gap-1.5">
              <span class="font-mono font-black text-sm text-white">SIM ${s.slot}</span>
              ${statusBadge}
              ${bootBadge}
              ${euiccBadge}
            </div>
            <span class="px-2 py-0.5 rounded border text-[10px] font-mono ${cpinCls}">${escapeHtml(cpinLabel)}</span>
          </div>

          <div class="flex items-start justify-between gap-2">
            <div>
              <div class="text-sm font-bold text-white">${escapeHtml(titleName)}</div>
              <div class="text-[11px] text-slate-400 mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5">
                <span class="text-cyan-300 font-medium">${escapeHtml(carrierStr)}</span>
                <span class="font-mono text-slate-500">接口: ${escapeHtml(s.iface)}</span>
                ${prof.phone ? `<span class="font-mono text-emerald-300">号码: ${escapeHtml(prof.phone)}</span>` : ''}
              </div>
              ${prof.notes ? `<div class="text-[11px] text-slate-400 mt-1">📝 ${escapeHtml(prof.notes)}</div>` : ''}
            </div>
            <button onclick="openSimProfileModal(${s.slot})" class="px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-cyan-300 border border-slate-700 text-[11px] shrink-0 transition">
              ✏️ 档案/限额
            </button>
          </div>

          <div class="p-2.5 rounded-xl bg-slate-950/80 border border-slate-800/80 space-y-2">
            <div class="flex items-center justify-between text-[11px]">
              <span class="text-slate-400">卡片 ICCID</span>
              ${s.iccid
                ? `<button onclick="copyText('${escapeJs(s.iccid)}')" class="font-mono text-slate-200 hover:text-cyan-300 underline decoration-dotted" title="点击复制完整 20 位 ICCID">${escapeHtml(s.iccid_masked)} 📋</button>`
                : '<span class="font-mono text-slate-500">未读取 (点击下方检测卡槽触点)</span>'}
            </div>
            <div class="space-y-1">
              <div class="flex items-center justify-between text-[11px]">
                <span class="text-slate-400">本卡独立账期已用 (今日 ${formatBytes(tr.today_bytes || 0)})</span>
                <span class="font-mono font-semibold text-white">${formatBytes(usedBytes)} / ${capBytes > 0 ? formatBytes(capBytes) : '不限额'} ${overTag}</span>
              </div>
              ${capBytes > 0 ? `
                <div class="w-full h-1.5 bg-slate-800 rounded-full overflow-hidden">
                  <div class="h-full ${usedPct >= 90 ? 'bg-rose-500' : (usedPct >= 75 ? 'bg-amber-400' : 'bg-cyan-400')}" style="width:${usedPct}%"></div>
                </div>
              ` : ''}
            </div>
          </div>

          <!-- 射频 / VoLTE / VoNR 独立快捷开关 -->
          <div class="flex flex-wrap items-center gap-2 text-[11px]">
            <button onclick="toggleSlotRadio(${s.slot}, ${!s.radio_enabled})" class="px-2.5 py-1 rounded-lg border font-mono transition ${s.radio_enabled ? 'bg-emerald-500/15 border-emerald-500/35 text-emerald-300' : 'bg-slate-800 border-slate-700 text-slate-400'}">
              射频: ${s.radio_enabled ? '开启' : '关闭'}
            </button>
            <button onclick="toggleSlotVoice(${s.slot}, 'volte', ${!s.volte})" class="px-2.5 py-1 rounded-lg border font-mono transition ${s.volte ? 'bg-cyan-500/15 border-cyan-500/35 text-cyan-300' : 'bg-slate-800 border-slate-700 text-slate-400'}">
              VoLTE: ${s.volte ? '开' : '关'}
            </button>
            <button onclick="toggleSlotVoice(${s.slot}, 'vonr', ${!s.vonr})" class="px-2.5 py-1 rounded-lg border font-mono transition ${s.vonr ? 'bg-indigo-500/20 border-indigo-500/40 text-indigo-300' : 'bg-slate-800 border-slate-700 text-slate-400'}">
              5G VoNR: ${s.vonr ? '开' : '关'}
            </button>
            <span class="text-slate-500 font-mono ml-auto">APN: ${escapeHtml((s.apn_conf && s.apn_conf.apn) || '自动默认')}</span>
          </div>

          ${probeHtml}
        </div>

        <!-- 底部核心操作按钮组 -->
        <div class="flex flex-wrap items-center gap-2 pt-2.5 border-t border-slate-800/80">
          <button onclick="probeSimSlot(${s.slot})" id="btn-probe-slot-${s.slot}" class="px-3 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-cyan-300 border border-slate-700 font-semibold transition">
            🔍 检测卡槽触点 (不断网)
          </button>
          ${!isAct ? `
            <button onclick="switchActiveSim(${s.slot})" class="px-3 py-1.5 rounded-xl bg-emerald-500 hover:bg-emerald-400 text-black font-bold transition">
              ⚡ 切换至此卡上网
            </button>
            <button onclick="openFlashSwitchModal(${s.slot})" class="px-3 py-1.5 rounded-xl bg-amber-500/20 hover:bg-amber-500/30 text-amber-300 border border-amber-500/40 font-semibold transition" title="临时切往此卡收验证码，超时或收到短信自动切回主卡">
              ⏱️ 闪切接码
            </button>
          ` : `
            <span class="px-3 py-1.5 rounded-xl bg-emerald-500/10 text-emerald-300 border border-emerald-500/25 font-semibold">
              ✅ 当前正在承担全机流量
            </span>
          `}
          ${!s.boot_default ? `
            <button onclick="setDefaultSimSlot(${s.slot})" class="px-2.5 py-1.5 rounded-xl bg-slate-800/80 hover:bg-slate-700 text-slate-300 border border-slate-700 transition ml-auto">
              设为开机默认
            </button>
          ` : ''}
        </div>
      </div>
    `;
  }).join('');
}

async function enableDualSimHot() {
  showToast('正在启动 SIM 2 独立 AT Broker (nr4/nr5) 并开启热切就绪态...');
  const res = await apiCall('sim.enable_live');
  if (res && res.ok) {
    showToast(res.msg || '双卡在线热切通道已激活！');
    loadSimOverview(true);
  } else {
    showToast('激活失败: ' + ((res && res.msg) || ''), false);
  }
}

async function probeSimSlot(slotNum) {
  const btn = document.getElementById(`btn-probe-slot-${slotNum}`);
  if (btn) { btn.disabled = true; btn.innerText = '🔍 正在读取触点...'; }
  showToast(`正在无损探测 SIM ${slotNum} 物理触点、ICCID 与 eUICC ISD-R 通道（主卡不断网）...`);
  try {
    const res = await apiCall('sim.probe_slot', { slot: slotNum, power_sim: true, check_euicc: true });
    if (res && res.ok) {
      const okContact = !!res.contact_ok;
      const aidStr = (res.aids && res.aids.length)
        ? res.aids.map(a => `${a.type}(${a.aid.slice(0, 10)}..)`).join(', ')
        : '标准 USIM';
      const euiccLine = res.is_euicc
        ? '✨ 检测到 ISD-R (A0000005591010...)：支持 9eSIM / ESTK / 5ber 写卡管理！'
        : '普通实体 SIM / USIM 卡（未检出 eUICC ISD-R 容器）';
      slotProbeOutputs[slotNum] = `
        <div class="p-2.5 rounded-xl border font-mono text-[11px] space-y-1 ${okContact ? 'bg-emerald-950/30 border-emerald-500/40 text-emerald-200' : 'bg-rose-950/30 border-rose-500/40 text-rose-200'}">
          <div class="font-bold font-sans">${okContact ? '✅ 8 触点通信全部正常！' : '❌ 未检测到有效 SIM 响应（请检查卡托方向或焊接触点）'}</div>
          <div>CPIN: ${escapeHtml(res.cpin || '--')} · 运营商: ${escapeHtml(res.carrier || '--')}</div>
          <div>ICCID: ${escapeHtml(res.iccid_masked || '无')} · IMSI: ${escapeHtml(res.imsi_masked || '无')}</div>
          <div>应用目录: ${escapeHtml(aidStr)}</div>
          <div class="text-cyan-300 font-sans">${escapeHtml(euiccLine)}</div>
        </div>
      `;
      showToast(okContact ? `SIM ${slotNum} 触点与读卡检测通过！` : `SIM ${slotNum} 未读到卡片 (${res.cpin})`, okContact);
      await loadSimOverview(false);
    } else {
      showToast('探测失败: ' + ((res && res.error) || ''), false);
    }
  } finally {
    if (btn) { btn.disabled = false; btn.innerText = '🔍 检测卡槽触点 (不断网)'; }
  }
}

async function switchActiveSim(targetSlot) {
  const msg = `确定立即将蜂窝上网主卡热切换至【SIM ${targetSlot} (${targetSlot === 2 ? 'sipa_eth8' : 'sipa_eth0'})】吗？\n\n` +
    `• 若改装后您在卡 2 插入了流量卡，切换后全机 Wi-Fi 与 Tailscale 将立即改走卡 ${targetSlot} 流量；\n` +
    `• 安全防失联保护：若 SIM ${targetSlot} 在 45 秒内未能拨号获取 IP，系统将自动回退回原卡！`;
  if (!confirm(msg)) return;
  showToast(`已下发切卡任务：正在热切换至 SIM ${targetSlot}...`);
  const res = await apiCall('sim.switch', { slot: targetSlot, wan_wait: 45, make_default: false });
  if (res && res.ok) {
    startSimSwitchPolling();
  } else {
    showToast('启动切卡失败: ' + ((res && res.error) || ''), false);
  }
}

function startSimSwitchPolling() {
  if (simSwitchPollTimer) return;
  simSwitchPollTimer = setInterval(async () => {
    const st = await apiCall('sim.switch_status');
    if (!st || !st.ok) return; // 切卡期间短暂重连容错，继续等待
    const sw = st.switch || {};
    const fl = st.flash || {};
    renderSimSwitchBanner(sw, fl);
    if (!sw.running && !fl.running) {
      clearInterval(simSwitchPollTimer);
      simSwitchPollTimer = null;
      if (fl.phase === 'done' && fl.finished_at) {
        showToast(fl.msg || '闪切接码已完成并恢复主卡网络');
      } else if (sw.finished_at) {
        showToast(sw.msg || '双卡切换流程已结束', !!sw.ok);
      }
      loadNetworkTab();
      fetchOverview(true);
    }
  }, 2500);
}

function openFlashSwitchModal(targetSlot) {
  document.getElementById('sim-flash-target-slot').value = String(targetSlot);
  document.getElementById('sim-flash-modal').classList.remove('hidden');
}

function closeFlashSwitchModal() {
  document.getElementById('sim-flash-modal').classList.add('hidden');
}

async function submitFlashSwitch() {
  const targetSlot = parseInt(document.getElementById('sim-flash-target-slot').value, 10) || 2;
  const durationSec = parseInt(document.getElementById('sim-flash-duration').value, 10) || 120;
  const returnOnSms = document.getElementById('sim-flash-early-return').checked;
  const radioOnly = document.getElementById('sim-flash-radio-only').checked;
  closeFlashSwitchModal();
  showToast(`正在启动 SIM ${targetSlot} 闪切接码模式（最长 ${durationSec}s 后自动切回）...`);
  const res = await apiCall('sim.flash_switch', {
    slot: targetSlot,
    duration_sec: durationSec,
    return_on_sms: returnOnSms,
    radio_only: radioOnly,
  });
  if (res && res.ok) {
    startSimSwitchPolling();
  } else {
    showToast('启动闪切失败: ' + ((res && res.error) || ''), false);
  }
}

async function cancelFlashSwitch() {
  showToast('正在通知后台提前结束监听并切回主流量卡...');
  await apiCall('sim.flash_cancel');
}

async function setDefaultSimSlot(slotNum) {
  const res = await apiCall('sim.select', { slot: slotNum });
  if (res && res.ok) {
    showToast(`已将 SIM ${slotNum} 设为开机默认主卡`);
    loadSimOverview(false);
  } else {
    showToast('设置默认卡失败: ' + ((res && res.error) || ''), false);
  }
}

async function toggleSlotRadio(slotNum, turnOn) {
  if (!turnOn && simOverviewCache && simOverviewCache.active_slot === slotNum) {
    if (!confirm(`SIM ${slotNum} 是当前上网主卡，关闭其射频会暂停蜂窝网络，确定继续？`)) return;
  }
  showToast(`正在${turnOn ? '开启' : '关闭'} SIM ${slotNum} 射频...`);
  const res = await apiCall('radio.set', { slot: slotNum, state: turnOn ? 'on' : 'off' });
  if (res && res.ok) {
    showToast(`SIM ${slotNum} 射频已${turnOn ? '开启' : '关闭'}`);
    loadSimOverview(false);
  } else {
    showToast('操作失败: ' + ((res && res.error) || ''), false);
  }
}

async function toggleSlotVoice(slotNum, kind, turnOn) {
  const label = kind === 'vonr' ? '5G VoNR' : 'VoLTE';
  showToast(`正在设置 SIM ${slotNum} ${label}: ${turnOn ? '开启' : '关闭'}...`);
  const res = await apiCall('voice.set', { slot: slotNum, kind, state: turnOn ? 'on' : 'off' });
  if (res && res.ok) {
    showToast(`SIM ${slotNum} ${label} 已${turnOn ? '开启' : '关闭'}`);
    loadSimOverview(false);
  } else {
    showToast('设置失败: ' + ((res && res.error) || ''), false);
  }
}

function openSimProfileModal(slotNum) {
  const s = (simOverviewCache && Array.isArray(simOverviewCache.slots))
    ? simOverviewCache.slots.find(x => x.slot === slotNum)
    : null;
  if (!s || !s.iccid) {
    showToast(`请先点击 SIM ${slotNum} 的「🔍 检测卡槽触点」读取卡片 ICCID 后再编辑档案`, false);
    return;
  }
  const p = s.profile || {};
  const tr = s.traffic || {};
  document.getElementById('sim-prof-slot').value = String(slotNum);
  document.getElementById('sim-prof-raw-iccid').value = s.iccid;
  document.getElementById('sim-prof-modal-title').innerText = `编辑 SIM ${slotNum} 卡片档案与独立流量策略`;
  document.getElementById('sim-prof-modal-iccid').innerText = `绑定 ICCID: ${s.iccid_masked} (${s.carrier || '自动识别'})`;
  document.getElementById('sim-prof-nickname').value = p.nickname || '';
  document.getElementById('sim-prof-phone').value = p.phone || '';
  document.getElementById('sim-prof-carrier').value = p.carrier_custom || '';
  document.getElementById('sim-prof-notes').value = p.notes || '';
  document.getElementById('sim-prof-reset-day').value = p.reset_day || tr.reset_day || 1;
  const capGb = p.cap_gb || (tr.cap_bytes ? +(tr.cap_bytes / 1073741824).toFixed(1) : 0);
  document.getElementById('sim-prof-cap-gb').value = capGb;
  document.getElementById('sim-prof-daily-gb').value = p.daily_gb || 0;
  document.getElementById('sim-prof-warn-pct').value = p.warn_pct || tr.warn_pct || 85;
  document.getElementById('sim-prof-over-action').value = p.over_limit_action || 'none';
  document.getElementById('sim-profile-modal').classList.remove('hidden');
}

function closeSimProfileModal() {
  document.getElementById('sim-profile-modal').classList.add('hidden');
}

async function submitSimProfile() {
  const req = {
    slot: parseInt(document.getElementById('sim-prof-slot').value, 10) || 1,
    iccid: document.getElementById('sim-prof-raw-iccid').value,
    nickname: document.getElementById('sim-prof-nickname').value.trim(),
    phone: document.getElementById('sim-prof-phone').value.trim(),
    carrier_custom: document.getElementById('sim-prof-carrier').value.trim(),
    notes: document.getElementById('sim-prof-notes').value.trim(),
    reset_day: parseInt(document.getElementById('sim-prof-reset-day').value, 10) || 1,
    cap_gb: parseFloat(document.getElementById('sim-prof-cap-gb').value) || 0,
    daily_gb: parseFloat(document.getElementById('sim-prof-daily-gb').value) || 0,
    warn_pct: parseInt(document.getElementById('sim-prof-warn-pct').value, 10) || 85,
    over_limit_action: document.getElementById('sim-prof-over-action').value || 'none',
  };
  showToast('正在保存 SIM 卡片档案与独立流量策略...');
  const res = await apiCall('sim.card_save', req);
  if (res && res.ok) {
    closeSimProfileModal();
    showToast(res.msg || 'SIM 卡片档案已保存');
    loadSimOverview(false);
    fetchOverview(true);
  } else {
    showToast('保存失败: ' + ((res && res.error) || ''), false);
  }
}

// =========================================================================
// 移动网络 Tab 完整实现 (双卡中心 + 5步体检 + IP读取 + 频段锁定 + 分卡槽APN)
// =========================================================================
async function loadSlotApnForm(slotVal) {
  const sl = parseInt(slotVal, 10) || 1;
  const res = await apiCall('data.get', { slot: sl });
  if (!res || !res.ok) return;
  if (document.getElementById('apn-name')) document.getElementById('apn-name').value = res.apn || '';
  if (document.getElementById('apn-pdp') && res.pdp) document.getElementById('apn-pdp').value = res.pdp;
  if (document.getElementById('apn-auth') && res.auth) document.getElementById('apn-auth').value = res.auth;
  if (document.getElementById('apn-user')) document.getElementById('apn-user').value = res.user || '';
  if (document.getElementById('apn-pass')) document.getElementById('apn-pass').value = '';
  const passHint = document.getElementById('apn-pass-hint');
  if (passHint) passHint.innerText = res.password_set ? '(已设置密码)' : '';
  const roamCb = document.getElementById('apn-roaming');
  if (roamCb && res.roaming) {
    roamCb.checked = (Array.isArray(res.roaming.slots) && res.roaming.slots.length >= sl)
      ? !!res.roaming.slots[sl - 1]
      : !!res.roaming.allowed;
  }
}

async function loadNetworkTab() {
  loadSimOverview(false);
  loadCellularHealth(false);

  const res = await apiCall('data.get');
  if (res && res.ok) {
    const localIpv4 = res.ipv4 || (res.address ? res.address.split('/')[0] : '');
    const pubIpv4 = res.public_ipv4 || '';
    const netIpEl = document.getElementById('net-ip');
    if (netIpEl) netIpEl.innerText = pubIpv4 || localIpv4 || '--';
    const netLocalIpEl = document.getElementById('net-local-ip');
    if (netLocalIpEl) netLocalIpEl.innerText = localIpv4 || '--';
    const netIpv6El = document.getElementById('net-ipv6');
    if (netIpv6El) netIpv6El.innerText = res.ipv6 || '--';
    const netIfaceEl = document.getElementById('net-iface');
    if (netIfaceEl) netIfaceEl.innerText = res.iface || 'sipa_eth0';

    if (res.signal) {
      const netRatEl = document.getElementById('net-rat');
      if (netRatEl) netRatEl.innerText = (res.signal.rat || '5G') + ' SA';
      const netOpEl = document.getElementById('net-op');
      if (netOpEl) netOpEl.innerText = res.signal.operator === '46000' ? '46000 (中国移动)' : (res.signal.operator || '--');
      const netSigEl = document.getElementById('net-signal');
      if (netSigEl) netSigEl.innerText = (res.signal.rsrp || '--') + ' dBm / ' + (res.signal.rsrq || '--') + ' dB';
    }
    const netRoamEl = document.getElementById('net-roaming');
    if (netRoamEl && res.roaming) {
      netRoamEl.innerText = res.roaming.allowed ? '已允许' : '已关闭';
      netRoamEl.className = res.roaming.allowed ? 'font-bold text-emerald-400 mt-0.5' : 'font-bold text-amber-400 mt-0.5';
    }

    const btnData = document.getElementById('btn-toggle-data');
    if (btnData) {
      btnData.innerText = res.state === 'up' ? '数据已开启' : '数据已关闭';
      btnData.className = res.state === 'up'
        ? 'px-3 py-1 rounded-lg bg-emerald-500 text-black font-semibold text-xs transition'
        : 'px-3 py-1 rounded-lg bg-slate-800 text-slate-400 font-semibold text-xs border border-slate-700 transition';
    }

    const targetSlotEl = document.getElementById('apn-target-slot');
    const selSlot = targetSlotEl ? parseInt(targetSlotEl.value, 10) : (res.slot || 1);
    if (selSlot === (res.slot || 1) && document.getElementById('apn-name')) {
      document.getElementById('apn-name').value = res.apn || '';
      if (res.pdp) document.getElementById('apn-pdp').value = res.pdp;
      if (res.auth) document.getElementById('apn-auth').value = res.auth;
      if (document.getElementById('apn-user')) document.getElementById('apn-user').value = res.user || '';
      if (document.getElementById('apn-roaming') && res.roaming) document.getElementById('apn-roaming').checked = !!res.roaming.allowed;
    }
  }

  // 读取频段锁定 (lock.get)
  const lockRes = await apiCall('lock.get');
  if (lockRes && lockRes.data) {
    const nrLocked = String(lockRes.data.nr ? (lockRes.data.nr.locked || '') : '');
    const lteLocked = String(lockRes.data.lte ? (lockRes.data.lte.locked || '') : '');
    const modeVal = lockRes.data.mode ? (lockRes.data.mode.label || lockRes.data.mode.value || lockRes.data.mode.current || 'auto') : 'auto';
    if (modeVal && document.getElementById('net-lock-mode')) {
      document.getElementById('net-lock-mode').value = modeVal;
    }
    const lockBadge = document.getElementById('net-lock-badge');
    if (lockBadge) {
      if (nrLocked) {
        lockBadge.innerText = `已锁 NR: n${nrLocked.replace(/,/g, '/n')}`;
        lockBadge.className = 'px-2 py-0.5 rounded text-[10px] font-mono bg-amber-500/15 text-amber-300 border border-amber-500/30';
      } else {
        lockBadge.innerText = 'NR 全频段自动';
        lockBadge.className = 'px-2 py-0.5 rounded text-[10px] font-mono bg-cyan-500/15 text-cyan-300 border border-cyan-500/30';
      }
    }

    document.querySelectorAll('input[name="band-nr"]').forEach(cb => {
      cb.checked = !nrLocked || nrLocked.split(',').includes(cb.value);
    });
    document.querySelectorAll('input[name="band-lte"]').forEach(cb => {
      cb.checked = !lteLocked || lteLocked.split(',').includes(cb.value);
    });
  }

  // 获取签约状态与实时射频
  try {
    const qosResp = await fetch('/api/cellular_qos');
    if (qosResp.ok) {
      const qres = await qosResp.json();
      if (qres.ok && qres.data) {
        const qos = qres.data;
        const elNetOpName = document.getElementById('net-operator-name');
        if (elNetOpName) elNetOpName.innerText = qos.operator_name || '中国移动';
        const elNetRatBadge = document.getElementById('net-rat-badge');
        if (elNetRatBadge) elNetRatBadge.innerText = qos.rat || '5G SA';
        const elNetBandBadge = document.getElementById('net-band-badge');
        if (elNetBandBadge) elNetBandBadge.innerText = qos.band || 'n41';
        const elNetQci = document.getElementById('net-qci');
        if (elNetQci) elNetQci.innerText = qos.qci ? ('QCI: ' + qos.qci) : 'QCI: 8';
        const elNetDl = document.getElementById('net-dl-rate');
        if (elNetDl) elNetDl.innerText = qos.dl_rate_str || '512 Mbps';
        const elNetUl = document.getElementById('net-ul-rate');
        if (elNetUl) elNetUl.innerText = qos.dl_rate_str ? qos.ul_rate_str : '60 Mbps';
        const elNetRsrp = document.getElementById('net-rsrp-val');
        if (elNetRsrp) elNetRsrp.innerText = (qos.rsrp || '--') + ' dBm';
        const elNetSinr = document.getElementById('net-sinr-val');
        if (elNetSinr) elNetSinr.innerText = (qos.sinr !== undefined ? qos.sinr : '8.0') + ' dB';
        const elNetRsrq = document.getElementById('net-rsrq-val');
        if (elNetRsrq) elNetRsrq.innerText = (qos.rsrq || '--') + ' dB';

        const rsrpNum = parseInt(qos.rsrp || -95);
        let activeBars = 4;
        if (rsrpNum >= -80) activeBars = 5;
        else if (rsrpNum >= -90) activeBars = 4;
        else if (rsrpNum >= -100) activeBars = 3;
        else if (rsrpNum >= -112) activeBars = 2;
        else activeBars = 1;
        const container = document.getElementById('net-sig-bars');
        if (container && container.children) {
          Array.from(container.children).forEach((bar, idx) => {
            if (idx < activeBars) {
              bar.className = bar.className.replace('bg-slate-700', 'bg-cyan-400');
            } else {
              bar.className = bar.className.replace('bg-cyan-400', 'bg-slate-700');
            }
          });
        }
      }
    }
  } catch (e) {}
}

async function toggleMobileData() {
  const btn = document.getElementById('btn-toggle-data');
  const isUp = btn && btn.innerText.includes('开启');
  showToast(isUp ? '正在关闭蜂窝移动数据...' : '正在开启蜂窝移动数据...');
  await apiCall('data.power', { state: isUp ? 'off' : 'on' });
  setTimeout(loadNetworkTab, 2500);
}

async function saveBandLock() {
  const allNr = Array.from(document.querySelectorAll('input[name="band-nr"]'));
  const nrBands = allNr.filter(cb => cb.checked).map(cb => cb.value);
  const allLte = Array.from(document.querySelectorAll('input[name="band-lte"]'));
  const lteBands = allLte.filter(cb => cb.checked).map(cb => cb.value);

  showToast('正在向调制解调器下发频段锁定...');
  const nrVal = (nrBands.length === 0 || nrBands.length === allNr.length) ? '' : nrBands.join(',');
  const lteVal = (lteBands.length === 0 || lteBands.length === allLte.length) ? '' : lteBands.join(',');
  const r1 = await apiCall('lock.set', { kind: 'nr', val: nrVal });
  const r2 = await apiCall('lock.set', { kind: 'lte', val: lteVal });
  if ((r1 && r1.ok) || (r2 && r2.ok)) {
    showToast(`频段配置已生效 (NR: ${!nrVal ? '全频段自动' : 'n' + nrVal.replace(/,/g, '/n')})`);
    setTimeout(loadNetworkTab, 1500);
    setTimeout(fetchOverview, 2000);
  } else {
    showToast('频段锁定失败: ' + ((r1 && r1.error) || (r2 && r2.error) || ''), false);
  }
}

async function quickLockBand(preset) {
  if (preset === 'unlock') {
    showToast('正在恢复全频段自动选网...');
    const res = await apiCall('lock.set', { kind: 'reset', val: 'auto' });
    if (res && res.ok) {
      showToast('已恢复全频段自动选网');
      loadNetworkTab();
      setTimeout(fetchOverview, 1500);
    } else {
      showToast('恢复失败: ' + (res.error || ''), false);
    }
    return;
  }
  const targetList = String(preset).split(',');
  document.querySelectorAll('input[name="band-nr"]').forEach(cb => {
    cb.checked = targetList.includes(cb.value);
  });
  const labelStr = 'n' + targetList.join('+n');
  showToast(`正在快速锁定 5G 频段: ${labelStr}...`);
  const res = await apiCall('lock.set', { kind: 'nr', val: preset });
  if (res && res.ok) {
    showToast(`已锁定 5G ${labelStr} 频段`);
    setTimeout(loadNetworkTab, 1500);
    setTimeout(fetchOverview, 2000);
  } else {
    showToast('锁频失败: ' + (res.error || ''), false);
  }
}

async function applyNetMode(mode) {
  showToast(`正在切换网络制式为: ${mode}...`);
  const res = await apiCall('lock.set', { kind: 'mode', val: mode });
  if (res && res.ok) {
    showToast('网络制式切换成功');
    setTimeout(loadNetworkTab, 2000);
  } else {
    showToast('制式切换失败: ' + (res.error || ''), false);
  }
}

async function saveApnConfig() {
  const slot = parseInt((document.getElementById('apn-target-slot') && document.getElementById('apn-target-slot').value) || '1', 10) || 1;
  const apn = document.getElementById('apn-name').value.trim();
  const pdp = document.getElementById('apn-pdp').value;
  const auth = document.getElementById('apn-auth').value;
  const user = (document.getElementById('apn-user') && document.getElementById('apn-user').value.trim()) || '';
  const pass = (document.getElementById('apn-pass') && document.getElementById('apn-pass').value) || '';
  const roaming = document.getElementById('apn-roaming') ? document.getElementById('apn-roaming').checked : true;

  const req = { slot, apn, pdp, auth, user, roaming };
  if (pass) req.password = pass;

  const isAct = !simOverviewCache || simOverviewCache.active_slot === slot;
  showToast(isAct ? `正在保存 SIM ${slot} APN 并重拨生效...` : `正在预存 SIM ${slot} 专属 APN 与漫游设定 (不中断当前主卡网络)...`);
  const res = await apiCall('data.set', req);
  if (res && res.ok) {
    if (document.getElementById('apn-pass')) document.getElementById('apn-pass').value = '';
    showToast(isAct ? `SIM ${slot} APN 已应用并正在重连` : `SIM ${slot} 专属 APN 与漫游配置已保存，切换至该卡时将自动生效`);
    setTimeout(() => { loadNetworkTab(); }, isAct ? 3000 : 600);
  } else {
    showToast('保存失败: ' + (res.error || ''), false);
  }
}

// =========================================================================
// 流量统计 Tab 完整实现 (按 ICCID 独立账本 + 超额自动切备用 SIM 卡联动)
// =========================================================================
const GIB_BYTES = 1073741824;

async function loadTrafficStats() {
  const tbody = document.getElementById('traffic-history-tbody');
  if (tbody) tbody.innerHTML = '<tr><td colspan="4" class="py-4 text-center text-slate-500 font-sans">正在读取流量记录...</td></tr>';

  const [res, simOv] = await Promise.all([apiCall('traffic.get'), apiCall('sim.overview')]);
  let activeSlotObj = null;
  if (simOv && simOv.ok && Array.isArray(simOv.slots)) {
    simOverviewCache = simOv;
    activeSlotObj = simOv.slots.find(x => x.active) || null;
    const badgeEl = document.getElementById('traffic-active-sim-badge');
    if (badgeEl && activeSlotObj) {
      const nick = (activeSlotObj.profile && activeSlotObj.profile.nickname) || `SIM ${activeSlotObj.slot}`;
      badgeEl.innerText = `当前账本绑定: ${nick} · ${activeSlotObj.iccid_masked || 'ICCID未读'} (${activeSlotObj.iface})`;
    }
  }

  if (res && res.traffic) {
    const t = res.traffic;
    const resetInput = document.getElementById('traffic-reset-day');
    if (resetInput) resetInput.value = t.reset_day || 1;

    const capBytes = (t.cap && t.cap.bytes) ? Number(t.cap.bytes) : 0;
    const capGb = capBytes > 0 ? +(capBytes / GIB_BYTES).toFixed(1) : 0;
    const limitInput = document.getElementById('traffic-limit-gb');
    if (limitInput) limitInput.value = capGb;

    const dailyBytes = (t.daily && t.daily.bytes) ? Number(t.daily.bytes) : 0;
    const dailyGb = dailyBytes > 0 ? +(dailyBytes / GIB_BYTES).toFixed(1) : ((activeSlotObj && activeSlotObj.profile && activeSlotObj.profile.daily_gb) || 0);
    const dailyInput = document.getElementById('traffic-daily-gb');
    if (dailyInput) dailyInput.value = dailyGb;

    const warnInput = document.getElementById('traffic-warn-pct');
    if (warnInput) warnInput.value = (t.cap && t.cap.warn) ? Number(t.cap.warn) : 80;

    const overSelect = document.getElementById('traffic-over-action');
    if (overSelect) {
      const profAct = activeSlotObj && activeSlotObj.profile && activeSlotObj.profile.over_limit_action;
      if (profAct && profAct !== 'none') {
        overSelect.value = profAct;
      } else {
        overSelect.value = (t.cap && t.cap.cut) ? 'cut_data' : 'none';
      }
    }

    if (tbody) {
      const days = t.days || [];
      if (days.length > 0) {
        tbody.innerHTML = days.map(d => {
          const rx = Number(d.rx || 0);
          const tx = Number(d.tx || 0);
          const total = rx + tx;
          return `
            <tr class="hover:bg-slate-800/40 transition">
              <td class="py-2.5 font-bold text-white font-sans">${d.date}</td>
              <td class="py-2.5 text-cyan-400 font-mono">${formatBytes(rx)}</td>
              <td class="py-2.5 text-purple-400 font-mono">${formatBytes(tx)}</td>
              <td class="py-2.5 text-emerald-400 font-mono font-bold">${formatBytes(total)}</td>
            </tr>
          `;
        }).join('');
      } else {
        tbody.innerHTML = '<tr><td colspan="4" class="py-4 text-center text-slate-500 font-sans">暂无历史流量记录</td></tr>';
      }
    }
  } else {
    if (tbody) tbody.innerHTML = '<tr><td colspan="4" class="py-4 text-center text-slate-500 font-sans">读取流量记录失败</td></tr>';
  }
}

async function saveTrafficQuota() {
  const resetDay = parseInt(document.getElementById('traffic-reset-day').value, 10) || 1;
  const limitGb = parseFloat(document.getElementById('traffic-limit-gb').value) || 0;
  const dailyGb = parseFloat((document.getElementById('traffic-daily-gb') && document.getElementById('traffic-daily-gb').value) || 0) || 0;
  const warnPct = Math.min(99, Math.max(50, parseInt((document.getElementById('traffic-warn-pct') && document.getElementById('traffic-warn-pct').value) || 80, 10)));
  const overAction = (document.getElementById('traffic-over-action') && document.getElementById('traffic-over-action').value) || 'none';
  const cut = overAction === 'cut_data' ? 'on' : 'off';
  const capBytes = String(Math.round(limitGb * GIB_BYTES));
  const dailyBytes = String(Math.round(dailyGb * GIB_BYTES));

  showToast('正在保存当前 SIM 卡独立流量配额与超额保护策略...');
  await apiCall('traffic.set', { key: 'reset_day', value: resetDay });
  await apiCall('traffic.set', { key: 'cap', value: capBytes });
  await apiCall('traffic.set', { key: 'daily', value: dailyBytes });
  await apiCall('traffic.set', { key: 'warn', value: warnPct });
  await apiCall('traffic.set', { key: 'cut', value: cut });
  await apiCall('traffic.set', { key: 'daily_cut', value: cut });

  const activeSlotNum = (simOverviewCache && simOverviewCache.active_slot) || 1;
  await apiCall('sim.card_save', {
    slot: activeSlotNum,
    reset_day: resetDay,
    cap_gb: limitGb,
    daily_gb: dailyGb,
    warn_pct: warnPct,
    over_limit_action: overAction,
  });

  const actLabel = overAction === 'switch_sim' ? '超额自动热切备用卡' : (overAction === 'cut_data' ? '超额断网' : '仅预警');
  showToast(`已保存: 每月${resetDay}日重置，月限 ${limitGb} GB · 策略: ${actLabel}`);
  loadTrafficStats();
  fetchOverview(true);
}

// =========================================================================
// Wi-Fi / 电源 / 系统 Tab 完整实现
// =========================================================================
let currentBlocklist = [];
let currentLanSubnet = '192.168.78.';
let currentLanPolicies = { default_group: 'PROXY', clients: [] };

function toggleBlocklistPanel() {
  const panel = document.getElementById('blocklist-panel');
  if (panel) panel.classList.toggle('hidden');
}

async function fetchWifiClients(manual = false) {
  if (manual) showToast('正在刷新在线终端与策略状态...');
  try {
    const resp = await fetch('/api/wifi/clients' + (manual ? '?force=1' : ''));
    if (!resp.ok) return;
    const res = await resp.json();
    if (res && res.ok) {
      const clients = res.clients || [];
      currentBlocklist = res.blocklist || [];
      if (res.lan_subnet) currentLanSubnet = res.lan_subnet;
      if (res.policies) {
        currentLanPolicies = res.policies;
        renderLanPolicies(currentLanPolicies);
      }

      // 1. 更新在线数徽章
      const badge = document.getElementById('wifi-clients-badge');
      if (badge) {
        badge.innerText = `${clients.length} 台设备在线`;
        badge.className = clients.length > 0
          ? 'px-2.5 py-0.5 rounded-full text-[11px] font-bold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30'
          : 'px-2.5 py-0.5 rounded-full text-[11px] font-bold bg-slate-800 text-slate-400 border border-slate-700';
      }
      if (document.getElementById('blocklist-count')) {
        document.getElementById('blocklist-count').innerText = currentBlocklist.length;
      }

      // 2. 渲染在线终端列表
      const container = document.getElementById('wifi-clients-container');
      if (container) {
        if (clients.length === 0) {
          container.innerHTML = `
            <div class="text-center py-7 text-xs bg-slate-900/40 rounded-xl border border-slate-800/40 space-y-1">
              <div class="text-slate-400 font-medium">当前暂无无线设备连接至本热点</div>
              <div class="text-slate-500 font-mono text-[11px]">热点名称 (SSID): ${document.getElementById('wifi-ssid') ? document.getElementById('wifi-ssid').value : 'U30Air_5G'}</div>
            </div>`;
        } else {
          container.innerHTML = clients.map(c => {
            const defGrp = (currentLanPolicies && currentLanPolicies.default_group) || 'PROXY';
            const allGroups = currentVpnNodesData && currentVpnNodesData.groups ? currentVpnNodesData.groups.map(g => g.name) : [defGrp];
            const cycleList = [...allGroups, 'DIRECT'];
            const sig = c.signal !== null ? `${c.signal} dBm` : (c.online ? '在线良好' : '租约记录');
            const sigColor = (c.signal && c.signal >= -65) ? 'text-emerald-400' : ((c.signal && c.signal >= -78) ? 'text-cyan-400' : 'text-emerald-400');
            const pGroup = c.policy_group || defGrp;
            const isCustom = pGroup !== defGrp && pGroup !== 'DIRECT';
            const groupBadge = pGroup === 'DIRECT'
              ? '<span class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/40">⚡ 纯直连</span>'
              : (isCustom
                ? `<span class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-indigo-500/20 text-indigo-300 border border-indigo-500/40">🚀 ${escapeHtml(pGroup)}</span>`
                : `<span class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-cyan-500/15 text-cyan-300 border border-cyan-500/30">🚀 ${escapeHtml(pGroup)}</span>`);
            const staticBadge = c.is_static
              ? '<span class="px-1.5 py-0.5 rounded text-[10px] font-mono bg-amber-500/15 text-amber-300 border border-amber-500/30">📌 固定IP</span>'
              : '<span class="px-1.5 py-0.5 rounded text-[10px] font-mono bg-slate-800 text-slate-400 border border-slate-700">动态DHCP</span>';
            const connBadge = c.active_conns > 0
              ? `<span class="px-1.5 py-0.5 rounded text-[10px] font-mono bg-emerald-500/15 text-emerald-300 border border-emerald-500/30" title="Mihomo 实时活跃连接与命中策略">⚡ ${c.active_conns} 连接 · 命中:${escapeHtml(c.active_group || pGroup)}${c.active_node ? ' (' + escapeHtml(c.active_node) + ')' : ''}</span>`
              : '';
            const pendingIpNote = c.pending_rebind
              ? `<span class="text-amber-300 text-[11px]" title="已通过双 IP 映射立即按新策略分流；重连 Wi-Fi 后将切换至新固定 IP">(当前在线 IP: ${escapeHtml(c.live_ip)} 已同步分流)</span>`
              : '';
            const curIdx = cycleList.indexOf(pGroup);
            const nextGroup = cycleList[(curIdx + 1) % cycleList.length] || cycleList[0];
            const nextGroupBtnLabel = `🔄 切至 ${escapeHtml(nextGroup)}`;
            const nextGroupBtnCls = isCustom
              ? 'bg-cyan-500/15 hover:bg-cyan-500/25 text-cyan-300 border-cyan-500/30'
              : 'bg-indigo-500/20 hover:bg-indigo-500/30 text-indigo-300 border-indigo-500/40';

            return `
            <div class="p-3 sm:p-4 rounded-xl bg-slate-900/60 border border-slate-800/80 hover:border-slate-700 transition flex flex-col lg:flex-row lg:items-center justify-between gap-3">
              <div class="flex items-center gap-3">
                <div class="w-10 h-10 rounded-xl ${isCustom ? 'bg-indigo-500/15 border-indigo-500/30 text-indigo-300' : 'bg-cyan-500/10 border-cyan-500/20 text-cyan-400'} border flex items-center justify-center flex-shrink-0">
                  <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 18h.01M8 21h8a2 2 0 002-2V5a2 2 0 00-2-2H8a2 2 0 00-2 2v14a2 2 0 002 2z"></path></svg>
                </div>
                <div>
                  <div class="flex flex-wrap items-center gap-1.5">
                    <span class="text-sm font-bold text-white tracking-tight">${escapeHtml(c.name || '无线终端')}</span>
                    <span class="px-1.5 py-0.2 rounded text-[10px] font-medium bg-slate-800 text-cyan-300 border border-slate-700 font-mono">${escapeHtml(c.vendor)}</span>
                    ${groupBadge}
                    ${staticBadge}
                    ${connBadge}
                  </div>
                  <div class="flex flex-wrap items-center gap-x-3 gap-y-0.5 mt-1 text-xs text-slate-400 font-mono">
                    <span class="text-emerald-400 font-semibold">${c.ip || '待分配IP'}</span>
                    ${pendingIpNote}
                    <span class="text-slate-500">${c.mac}</span>
                    <span class="${sigColor} flex items-center gap-1 font-sans">
                      <span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
                      ${sig}
                    </span>
                  </div>
                </div>
              </div>

              <div class="flex flex-wrap items-center justify-between lg:justify-end gap-2 pt-2 lg:pt-0 border-t lg:border-t-0 border-slate-800/60">
                <button onclick="quickSetClientGroup('${c.mac}', '${c.ip || ''}', '${escapeJs(c.name || '')}', '${escapeJs(nextGroup)}')" class="px-2.5 py-1.5 rounded-lg border text-xs font-semibold transition ${nextGroupBtnCls}" title="一键将该终端绑定固定 IP 并切换分流组">
                  ${nextGroupBtnLabel}
                </button>
                <button onclick="prefillBindForm('${c.mac}', '${c.ip || ''}', '${escapeJs(c.name || '')}', '${escapeJs(pGroup)}')" class="px-2.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 text-xs font-medium transition">
                  📌 固定IP/分流
                </button>
                <button onclick="kickClient('${c.mac}', '${escapeJs(c.name)}')" class="px-2.5 py-1.5 rounded-lg bg-amber-500/10 hover:bg-amber-500/20 text-amber-400 border border-amber-500/30 text-xs font-medium transition">
                  踢下线
                </button>
                <button onclick="blockClient('${c.mac}', '${escapeJs(c.name)}', '${c.ip}')" class="px-2.5 py-1.5 rounded-lg bg-rose-500/10 hover:bg-rose-500/20 text-rose-400 border border-rose-500/30 text-xs font-medium transition">
                  拉黑
                </button>
              </div>
            </div>`;
          }).join('');
        }
      }

      // 3. 渲染黑名单列表
      renderBlocklist(currentBlocklist);
      if (manual) showToast(`终端列表已更新 (${clients.length} 台在线)`);
    }
  } catch (e) {
    console.error("fetchWifiClients error", e);
  }
}

function renderLanPolicies(policies) {
  const box = document.getElementById('lan-policies-list');
  if (!box) return;
  const list = (policies && policies.clients) || [];
  const defGrp = (policies && policies.default_group) || 'PROXY';
  const allGroups = currentVpnNodesData && currentVpnNodesData.groups ? currentVpnNodesData.groups.map(g => g.name) : [defGrp];
  const cycleList = [...allGroups, 'DIRECT'];
  const badge = document.getElementById('policy-rules-badge');
  if (badge) {
    badge.innerText = `已设 ${list.length} 条专属绑定 · 其余默认走 ${defGrp}`;
  }
  if (!list.length) {
    box.innerHTML = `<div class="text-slate-500 text-center py-3 bg-slate-900/30 rounded-xl border border-slate-800/50">暂无自定义固定 IP 分流规则，所有连接默认走【🚀 ${escapeHtml(defGrp)}】</div>`;
    return;
  }
  box.innerHTML = list.map(item => {
    const grp = item.group || defGrp;
    const grpBadge = grp === 'DIRECT'
      ? '<span class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 font-bold">⚡ 纯直连</span>'
      : `<span class="px-2 py-0.5 rounded bg-indigo-500/20 text-indigo-300 border border-indigo-500/30 font-bold">🚀 ${escapeHtml(grp)}</span>`;
    const curIdx = cycleList.indexOf(grp);
    const nextGrp = cycleList[(curIdx + 1) % cycleList.length] || cycleList[0];
    return `
      <div class="p-3 rounded-xl bg-slate-900/80 border border-slate-800 flex flex-col sm:flex-row sm:items-center justify-between gap-2">
        <div class="flex flex-wrap items-center gap-2">
          ${grpBadge}
          <span class="font-bold text-white">${escapeHtml(item.name || '绑定终端')}</span>
          <span class="font-mono text-emerald-400 font-semibold">${escapeHtml(item.ip || '未指定IP')}</span>
          ${item.mac ? `<span class="font-mono text-slate-400 text-[11px]">MAC: ${escapeHtml(item.mac)}</span>` : '<span class="text-slate-500 text-[11px]">(纯IP分流)</span>'}
        </div>
        <div class="flex items-center gap-2 self-end sm:self-auto">
          <button onclick="quickSetClientGroup('${escapeJs(item.mac || '')}', '${escapeJs(item.ip || '')}', '${escapeJs(item.name || '')}', '${escapeJs(nextGrp)}')" class="px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-cyan-300 border border-slate-700 transition">
            切至 ${escapeHtml(nextGrp)}
          </button>
          <button onclick="prefillBindForm('${escapeJs(item.mac || '')}', '${escapeJs(item.ip || '')}', '${escapeJs(item.name || '')}', '${escapeJs(grp)}')" class="px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 transition">
            编辑
          </button>
          <button onclick="removeClientBind('${escapeJs(item.mac || '')}', '${escapeJs(item.ip || '')}')" class="px-2.5 py-1 rounded-lg bg-rose-500/10 hover:bg-rose-500/20 text-rose-400 border border-rose-500/30 transition">
            删除
          </button>
        </div>
      </div>
    `;
  }).join('');
}

function fillQuickStaticIp(suffix) {
  const ipInput = document.getElementById('bind-dev-ip');
  if (ipInput) ipInput.value = `${currentLanSubnet}${suffix}`;
}

function clearBindForm() {
  if (document.getElementById('bind-dev-name')) document.getElementById('bind-dev-name').value = '';
  if (document.getElementById('bind-dev-mac')) document.getElementById('bind-dev-mac').value = '';
  if (document.getElementById('bind-dev-ip')) document.getElementById('bind-dev-ip').value = '';
}

function prefillBindForm(mac, ip, name, group) {
  if (document.getElementById('bind-dev-name')) document.getElementById('bind-dev-name').value = name || '';
  if (document.getElementById('bind-dev-mac')) document.getElementById('bind-dev-mac').value = mac || '';
  if (document.getElementById('bind-dev-ip')) document.getElementById('bind-dev-ip').value = ip || `${currentLanSubnet}10`;
  const sel = document.getElementById('bind-dev-group');
  if (sel && group) {
    if (!Array.from(sel.options).some(o => o.value === group)) {
      const opt = document.createElement('option');
      opt.value = group;
      opt.textContent = `🚀 ${group}`;
      sel.appendChild(opt);
    }
    sel.value = group;
  }
  const card = document.getElementById('lan-policy-card');
  if (card) card.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function quickSetClientGroup(mac, ip, name, group) {
  if (!ip && mac) {
    ip = `${currentLanSubnet}${Math.floor(10 + Math.random() * 80)}`;
  }
  showToast(`正在将【${name || ip || mac}】设为固定 IP (${ip}) 并分配至【${group}】...`);
  try {
    const resp = await fetch('/api/lan/bind', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        mac,
        ip,
        name,
        group,
        static_dhcp: !!mac,
        reconnect: false
      })
    });
    const res = await resp.json();
    if (res && res.ok) {
      showToast(res.msg || '分流策略已生效');
      fetchWifiClients(false);
    } else {
      showToast('设置失败: ' + ((res && res.error) || ''), false);
    }
  } catch (e) {
    showToast('请求失败', false);
  }
}

async function submitClientBind() {
  const name = (document.getElementById('bind-dev-name').value || '').trim();
  const mac = (document.getElementById('bind-dev-mac').value || '').trim().toLowerCase();
  const ip = (document.getElementById('bind-dev-ip').value || '').trim();
  const group = document.getElementById('bind-dev-group').value || 'PROXY';
  const reconnect = document.getElementById('bind-dev-reconnect') ? document.getElementById('bind-dev-reconnect').checked : false;

  if (!ip) return showToast('请填写要分配的固定局域网 IP (如 192.168.78.10)', false);
  showToast('正在保存固定 IP 绑定并同步 Mihomo 策略组分流...');
  try {
    const resp = await fetch('/api/lan/bind', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name,
        mac,
        ip,
        group,
        static_dhcp: !!mac,
        reconnect
      })
    });
    const res = await resp.json();
    if (res && res.ok) {
      showToast(res.msg || '固定 IP 与策略组分流已生效');
      clearBindForm();
      fetchWifiClients(false);
    } else {
      showToast('保存失败: ' + ((res && res.error) || ''), false);
    }
  } catch (e) {
    showToast('请求异常', false);
  }
}

async function removeClientBind(mac, ip) {
  if (!confirm(`确定移除设备 (${mac || ip}) 的固定 IP 与策略组绑定吗？移除后将恢复默认策略组。`)) return;
  showToast('正在移除绑定并更新分流规则...');
  try {
    const resp = await fetch('/api/lan/unbind', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mac, ip })
    });
    const res = await resp.json();
    if (res && res.ok) {
      showToast(res.msg || '已移除绑定');
      fetchWifiClients(false);
    } else {
      showToast('移除失败: ' + ((res && res.error) || ''), false);
    }
  } catch (e) {
    showToast('请求异常', false);
  }
}

function renderBlocklist(list) {
  const box = document.getElementById('blocklist-items');
  if (!box) return;
  if (!list || list.length === 0) {
    box.innerHTML = '<div class="text-slate-500 text-center py-2 text-xs">暂无被拉黑的设备</div>';
    return;
  }
  box.innerHTML = list.map(b => `
    <div class="p-2.5 rounded-lg bg-slate-900 border border-slate-800 flex items-center justify-between text-xs">
      <div>
        <div class="font-bold text-slate-200">${escapeHtml(b.name || '已封禁设备')}</div>
        <div class="text-[11px] text-slate-500 font-mono">${b.mac} · 拦截于 ${b.time || '--'}</div>
      </div>
      <button onclick="unblockClient('${b.mac}')" class="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-cyan-400 border border-slate-700 text-xs transition">
        解除拉黑
      </button>
    </div>
  `).join('');
}

async function kickClient(mac, name) {
  if (!confirm(`确认要将设备【${name || mac}】踢出 Wi-Fi 热点吗？`)) return;
  showToast('正在踢下线...');
  try {
    const resp = await fetch('/api/wifi/kick', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mac })
    });
    const res = await resp.json();
    showToast(res.msg || '设备已踢下线');
    setTimeout(() => fetchWifiClients(false), 800);
  } catch (e) {
    showToast('操作失败', false);
  }
}

async function blockClient(mac, name, ip) {
  if (!confirm(`警告：拉黑后将禁止设备【${name || mac}】连接网络并自动踢出，确认拉黑？`)) return;
  showToast('正在拉黑设备并拦截...');
  try {
    const resp = await fetch('/api/wifi/block', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mac, name, ip })
    });
    const res = await resp.json();
    showToast(res.msg || '设备已拉黑');
    setTimeout(() => fetchWifiClients(false), 800);
  } catch (e) {
    showToast('操作失败', false);
  }
}

async function unblockClient(mac) {
  showToast('正在解除拉黑...');
  try {
    const resp = await fetch('/api/wifi/unblock', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mac })
    });
    const res = await resp.json();
    showToast(res.msg || '已解除封禁');
    setTimeout(() => fetchWifiClients(false), 800);
  } catch (e) {
    showToast('操作失败', false);
  }
}

function escapeHtml(str) {
  return String(str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function escapeJs(str) {
  return String(str || '').replace(/'/g, "\\'");
}

async function loadWifiTab() {
  fetchWifiClients();
  loadNfcStatus(false);
  const res = await apiCall('wifi.get');
  if (res && res.ok) {
    if (document.getElementById('wifi-ssid')) document.getElementById('wifi-ssid').value = res.ssid || 'U30Air_5G';
    if (document.getElementById('wifi-band') && res.band) document.getElementById('wifi-band').value = res.band;
    const btn = document.getElementById('btn-wifi-toggle');
    if (btn) {
      btn.innerText = res.enabled ? 'Wi-Fi 已开启' : 'Wi-Fi 已关闭';
      btn.className = res.enabled
        ? 'px-3 py-1 rounded-lg bg-emerald-500 text-black font-semibold text-xs transition'
        : 'px-3 py-1 rounded-lg bg-slate-800 text-slate-400 font-semibold text-xs border border-slate-700 transition';
    }
  }
}

function renderNfcStatus(st) {
  if (!st) return;
  const card = document.getElementById('nfc-manage-card');
  if (st.supported === false) {
    if (card) card.classList.add('hidden');
    return;
  }
  if (card) card.classList.remove('hidden');

  const summaryEl = document.getElementById('nfc-summary-text');
  const hintEl = document.getElementById('nfc-rf-hint');
  const syncBadge = document.getElementById('nfc-sync-badge');
  const toggleBtn = document.getElementById('btn-nfc-toggle');

  if (summaryEl) summaryEl.innerText = st.summary || st.raw || '--';
  const rfOn = !!st.enabled;
  if (toggleBtn) {
    toggleBtn.dataset.enabled = rfOn ? 'true' : 'false';
    toggleBtn.innerText = rfOn ? 'NFC 射频已开启' : 'NFC 射频已关闭';
    toggleBtn.className = rfOn
      ? 'px-3 py-1 rounded-lg bg-emerald-500 hover:bg-emerald-400 text-black font-semibold text-xs transition'
      : 'px-3 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-400 font-semibold text-xs border border-slate-700 transition';
  }
  if (hintEl) {
    hintEl.innerText = rfOn
      ? '当前射频已开启：手机贴近机身即可读取标签（修改热点名称或密码后自动同步）'
      : '当前射频已静默关闭：手机贴近无任何响应（防公共场合他人碰机连网）';
  }
  if (syncBadge) {
    if (st.mode === 'wifi' && st.in_sync) {
      syncBadge.innerText = '已同步当前热点';
      syncBadge.className = 'px-2 py-0.5 rounded text-[11px] font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20';
    } else if (st.mode === 'wifi') {
      syncBadge.innerText = 'Wi-Fi 一碰连模式';
      syncBadge.className = 'px-2 py-0.5 rounded text-[11px] font-medium bg-cyan-500/10 text-cyan-400 border border-cyan-500/20';
    } else if (st.mode === 'url' || st.mode === 'text') {
      syncBadge.innerText = st.mode === 'url' ? '自定义网址模式' : '纯文本模式';
      syncBadge.className = 'px-2 py-0.5 rounded text-[11px] font-medium bg-amber-500/10 text-amber-300 border border-amber-500/20';
      const modeSel = document.getElementById('nfc-custom-mode');
      const valInput = document.getElementById('nfc-custom-val');
      if (modeSel) modeSel.value = st.mode;
      if (valInput && st.value && !valInput.value) valInput.value = st.value;
    } else {
      syncBadge.innerText = '空白标签';
      syncBadge.className = 'px-2 py-0.5 rounded text-[11px] font-medium bg-slate-800 text-slate-400 border border-slate-700';
    }
  }
}

async function loadNfcStatus(force = false) {
  const res = await apiCall('nfc.status', { force });
  if (res && res.ok) {
    renderNfcStatus(res);
  } else if (res && res.error) {
    const summaryEl = document.getElementById('nfc-summary-text');
    if (summaryEl) summaryEl.innerText = res.error;
    if (force) showToast(res.error, false);
  }
}

async function toggleNfcPower() {
  const btn = document.getElementById('btn-nfc-toggle');
  const curOn = btn ? btn.dataset.enabled === 'true' : true;
  const desired = !curOn;
  showToast(desired ? '正在开启 NFC 射频...' : '正在关闭 NFC 射频...');
  const res = await apiCall('nfc.toggle', { enable: desired });
  if (res && res.ok) {
    showToast(res.msg || 'NFC 状态已更新');
    if (res.status) renderNfcStatus(res.status);
    else loadNfcStatus(true);
  } else {
    showToast('切换失败: ' + ((res && res.error) || ''), false);
  }
}

async function writeNfcTag(mode) {
  if (mode === 'clear' && !confirm('确定清空 NFC 芯片内存储的数据吗？')) return;
  showToast(mode === 'wifi' ? '正在将当前 Wi-Fi 写入 NFC 芯片...' : '正在清空 NFC 标签...');
  const res = await apiCall('nfc.write', { mode });
  if (res && res.ok) {
    showToast(res.msg || 'NFC 写入成功');
    if (res.status) renderNfcStatus(res.status);
    else loadNfcStatus(true);
  } else {
    showToast('写入失败: ' + ((res && res.error) || ''), false);
  }
}

function onNfcCustomModeChange() {
  const mode = document.getElementById('nfc-custom-mode')?.value || 'url';
  const inp = document.getElementById('nfc-custom-val');
  if (inp) {
    inp.placeholder = mode === 'url'
      ? '如 http://192.168.78.1:8080/ 或 https://...'
      : '输入手机一碰显示的备忘文本';
  }
}

async function writeNfcCustom() {
  const mode = document.getElementById('nfc-custom-mode').value;
  const value = document.getElementById('nfc-custom-val').value.trim();
  if (!value) return showToast('请先输入要写入 NFC 的内容', false);
  showToast('正在写入自定义 NFC 内容...');
  const res = await apiCall('nfc.write', { mode, value });
  if (res && res.ok) {
    showToast(res.msg || '写入成功');
    if (res.status) renderNfcStatus(res.status);
    else loadNfcStatus(true);
  } else {
    showToast('写入失败: ' + ((res && res.error) || ''), false);
  }
}

async function saveWifiConfig() {
  const ssid = document.getElementById('wifi-ssid').value.trim();
  const pass = document.getElementById('wifi-pass').value;
  const band = document.getElementById('wifi-band').value;
  if (!ssid) return showToast('SSID 不能为空', false);
  showToast('正在保存 Wi-Fi 配置...');
  const req = { ssid, band };
  if (pass) req.password = pass;
  const res = await apiCall('wifi.set', req);
  if (res && res.ok) {
    showToast('Wi-Fi 配置已生效，正在自动同步 NFC 标签...');
    document.getElementById('wifi-pass').value = '';
    setTimeout(() => loadNfcStatus(true), 3500);
  } else {
    showToast('保存失败: ' + (res.error || ''), false);
  }
}

async function toggleWifiPower() {
  const btn = document.getElementById('btn-wifi-toggle');
  const isUp = btn && btn.innerText.includes('开启');
  showToast(isUp ? '正在关闭 Wi-Fi...' : '正在开启 Wi-Fi...');
  await apiCall('wifi.power', { state: isUp ? 'off' : 'on' });
  setTimeout(loadWifiTab, 2500);
}

async function scanWifiNetworks() {
  const box = document.getElementById('wifi-scan-results');
  box.innerHTML = '<div class="text-slate-400 text-center py-4 animate-pulse">正在请求无线网卡扫描周围 Wi-Fi...</div>';
  const res = await apiCall('wificlient.scan');
  let networks = (res && res.networks) || [];
  if (!networks.length && res && res.text) {
    const lines = res.text.trim().split('\n').slice(1);
    networks = lines.map(line => {
      const parts = line.trim().split(/\t+/);
      if (parts.length >= 5) {
        return { bssid: parts[0], freq: parts[1], signal: parts[2], flags: parts[3], ssid: parts.slice(4).join(' ') };
      }
      return null;
    }).filter(x => x && x.ssid);
  }
  if (networks.length) {
    box.innerHTML = networks.map(n => `
      <div class="p-2.5 bg-slate-900 border border-slate-800 rounded-xl flex items-center justify-between text-xs">
        <div>
          <div class="font-bold text-white">${escapeHtml(n.ssid)}</div>
          <div class="text-[11px] text-slate-400 font-mono">${escapeHtml(n.bssid || '')} · 信号: ${escapeHtml(n.signal || '--')} dBm</div>
        </div>
        <button onclick="promptJoinWifi('${escapeJs(n.ssid)}')" class="px-2.5 py-1 bg-cyan-600 hover:bg-cyan-500 text-black font-bold rounded-lg text-xs">连接</button>
      </div>
    `).join('');
  } else {
    const isApBusy = res && res.detail && (res.detail.includes('access point') || res.detail.includes('driver'));
    box.innerHTML = `
      <div class="p-3 rounded-xl bg-amber-500/10 border border-amber-500/30 text-xs space-y-2">
        <div class="text-amber-300 font-semibold">
          ${isApBusy ? '⚠️ 硬件限制提示：当前正开启 Wi-Fi 热点 (SoftAP)，展锐 SC2355 单无线网卡不支持在发射热点同时扫描周围 Wi-Fi。' : '未扫描到无线网络，您可直接手动输入上级 Wi-Fi 信息进行连接：'}
        </div>
        <div class="flex flex-col sm:flex-row gap-2 pt-1">
          <input type="text" id="manual-join-ssid" placeholder="上级 Wi-Fi 名称 (SSID)" class="flex-1 bg-slate-950 border border-slate-800 rounded-lg px-2.5 py-1.5 text-white text-xs">
          <input type="password" id="manual-join-psk" placeholder="Wi-Fi 密码 (开放留空)" class="flex-1 bg-slate-950 border border-slate-800 rounded-lg px-2.5 py-1.5 text-white text-xs">
          <button onclick="submitManualJoinWifi()" class="px-3 py-1.5 bg-cyan-600 hover:bg-cyan-500 text-black font-bold rounded-lg text-xs shrink-0">连接中继</button>
        </div>
      </div>
    `;
  }
}

async function submitManualJoinWifi() {
  const ssid = (document.getElementById('manual-join-ssid')?.value || '').trim();
  const psk = document.getElementById('manual-join-psk')?.value || '';
  if (!ssid) return showToast('请输入上级 Wi-Fi 名称 (SSID)', false);
  showToast(`正在配置连接上级 Wi-Fi: ${ssid}...`);
  const req = { ssid };
  if (psk) req.psk = psk;
  const res = await apiCall('wificlient.join', req);
  if (res && res.ok) {
    showToast(`已成功连接至 ${ssid}`);
  } else {
    showToast('连接失败: ' + ((res && (res.detail || res.error)) || ''), false);
  }
}

async function promptJoinWifi(ssid) {
  const psk = prompt(`请输入 Wi-Fi【${ssid}】的密码（开放网络留空）：`, '');
  if (psk === null) return;
  showToast(`正在连接上级 Wi-Fi: ${ssid}...`);
  const req = { ssid };
  if (psk) req.psk = psk;
  const res = await apiCall('wificlient.join', req);
  if (res && res.ok) {
    showToast(`已成功连接至 ${ssid}`);
  } else {
    showToast('连接失败: ' + ((res && (res.detail || res.error)) || ''), false);
  }
}

function renderBatteryStatus(b, prefix) {
  const badge = document.getElementById(prefix + '-bat-badge');
  const status = document.getElementById(prefix + '-bat-status');
  if (status) status.innerText = b.status_text || '充电状态未知';
  if (badge) {
    const tone = b.charging === true ? 'emerald' : b.state === 'full' ? 'cyan' : b.state === 'unknown' ? 'slate' : 'amber';
    const colors = {
      emerald: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
      cyan: 'bg-cyan-500/15 text-cyan-400 border-cyan-500/30',
      amber: 'bg-amber-500/15 text-amber-400 border-amber-500/30',
      slate: 'bg-slate-800 text-slate-400 border-slate-700'
    };
    badge.className = 'shrink-0 whitespace-nowrap px-2 py-1 rounded-full text-xs font-semibold flex items-center gap-1.5 border ' + colors[tone];
    badge.title = b.detail_text || '等待硬件状态';
    const dot = badge.querySelector('span');
    if (dot && dot !== status) dot.className = 'w-1.5 h-1.5 rounded-full bg-current' + (b.charging === true ? ' animate-pulse' : '');
  }
  const detail = prefix === 'pwr' ? document.getElementById('pwr-bat-detail') : null;
  if (detail) detail.innerText = b.detail_text || '等待硬件状态';
}

async function loadPowerTab() {
  // 实时电池状态
  try {
    const bResp = await fetch('/api/battery');
    if (bResp.ok) {
      const bData = await bResp.json();
      if (bData.ok && bData.data) {
        const b = bData.data;
        window._lastBatState = b;
        if (document.getElementById('pwr-bat-cap')) document.getElementById('pwr-bat-cap').innerText = b.capacity == null ? '—' : b.capacity + '%';
        if (document.getElementById('pwr-bat-volt')) document.getElementById('pwr-bat-volt').innerText = b.voltage_v == null ? '—' : Number(b.voltage_v).toFixed(3) + ' V';
        const currEl = document.getElementById('pwr-bat-curr');
        const pwrEl = document.getElementById('pwr-bat-pwr');
        if (currEl) {
          if (b.current_ma == null) {
            currEl.innerText = '—';
            if (pwrEl) pwrEl.innerText = '--';
          } else if (b.plugged && Math.abs(b.current_ma) <= 2 && b.charging === false) {
            currEl.innerText = '0 mA';
            currEl.className = 'text-base sm:text-lg font-bold font-mono text-cyan-300 mt-0.5';
            if (pwrEl) pwrEl.innerText = '零循环直供旁路';
          } else {
            const sign = b.current_ma > 0 ? '+' : '';
            currEl.innerText = `${sign}${b.current_ma} mA`;
            currEl.className = 'text-base sm:text-lg font-bold font-mono mt-0.5 ' + (b.current_ma > 0 ? 'text-emerald-400' : 'text-amber-400');
            if (pwrEl) pwrEl.innerText = b.power_mw != null ? `${(Math.abs(b.power_mw) / 1000).toFixed(2)} W` : '--';
          }
        }
        if (document.getElementById('pwr-bat-source')) {
          const ut = b.usb_type ? ` (${b.usb_type})` : '';
          document.getElementById('pwr-bat-source').innerText = b.plugged === true ? `插电${ut}` : (b.plugged === false ? '内置锂电' : '未知');
        }
        if (document.getElementById('pwr-bat-ilim')) {
          if (b.plugged && b.input_limit_ma) {
            const ichg = b.charging && b.charge_current_limit_ma ? ` · 充${b.charge_current_limit_ma}mA` : '';
            document.getElementById('pwr-bat-ilim').innerText = `输入 ${b.input_limit_ma}mA${ichg}`;
          } else {
            document.getElementById('pwr-bat-ilim').innerText = b.plugged ? 'USB 供电在线' : '电池独立放电';
          }
        }
        if (document.getElementById('pwr-bat-temp') && b.temp_c !== undefined && b.temp_c !== null) {
          document.getElementById('pwr-bat-temp').innerText = Number(b.temp_c).toFixed(1) + '°C';
        }
        if (document.getElementById('pwr-bat-soh')) {
          document.getElementById('pwr-bat-soh').innerText = b.soh_pct != null ? `${b.soh_pct}%` : '良好';
        }
        if (document.getElementById('pwr-bat-mah')) {
          if (b.charge_full_mah && b.charge_design_mah) {
            document.getElementById('pwr-bat-mah').innerText = `${b.charge_full_mah} / ${b.charge_design_mah}mAh`;
          } else {
            document.getElementById('pwr-bat-mah').innerText = '4500 mAh';
          }
        }
        renderBatteryStatus(b, 'pwr');
        const thmGrid = document.getElementById('pwr-thermal-grid');
        if (thmGrid && b.thermal && b.thermal.sensors) {
          thmGrid.innerHTML = b.thermal.sensors.map(s => {
            const t = Number(s.temp || 0);
            const color = t >= 62 ? 'text-rose-400' : (t >= 50 ? 'text-amber-400' : (t >= 40 ? 'text-cyan-300' : 'text-emerald-400'));
            const barColor = t >= 62 ? 'bg-rose-500' : (t >= 50 ? 'bg-amber-400' : 'bg-emerald-400');
            const pct = Math.min(100, Math.max(15, Math.round((t / 85) * 100)));
            return `
              <div class="p-3 rounded-xl bg-slate-900/70 border border-slate-800/80 space-y-1.5">
                <div class="flex items-center justify-between">
                  <span class="font-semibold text-white text-xs">${escapeHtml(s.name)}</span>
                  <span class="font-mono font-bold text-sm ${color}">${t.toFixed(1)}°C</span>
                </div>
                <div class="w-full bg-slate-800 rounded-full h-1 overflow-hidden">
                  <div class="${barColor} h-1 rounded-full transition-all" style="width: ${pct}%"></div>
                </div>
                <div class="text-[10px] text-slate-500 font-mono truncate">${escapeHtml(s.sub)}</div>
              </div>
            `;
          }).join('');
        }
      }
    }
  } catch (e) {}

  const res = await apiCall('power.get');
  if (res && res.ok) {
    const conf = res.conf || res;
    const chargeTo = Number(conf.CHARGE_TO !== undefined ? conf.CHARGE_TO : (res.charge_to || 80));
    const rawResume = Number(conf.RESUME_AT !== undefined ? conf.RESUME_AT : (res.resume_at || 0));
    const resumeAt = rawResume > 0 ? rawResume : Math.max(40, chargeTo - 5);
    const saverBelow = Number(conf.SAVER_BELOW !== undefined ? conf.SAVER_BELOW : (res.saver_below || 20));
    const fullOnce = Number(conf.FULL_ONCE !== undefined ? conf.FULL_ONCE : (res.full_once || 0)) === 1;
    const paused = Number(conf.CHARGE_PAUSE !== undefined ? conf.CHARGE_PAUSE : (res.charge_pause || 0)) === 1;
    window._powerConfState = { chargeTo, resumeAt, saverBelow, fullOnce, paused };

    if (document.getElementById('range-charge-cap')) {
      document.getElementById('range-charge-cap').value = chargeTo;
      document.getElementById('txt-charge-cap').innerText = chargeTo + '%';
    }
    if (document.getElementById('range-resume-cap')) {
      document.getElementById('range-resume-cap').value = resumeAt;
      document.getElementById('txt-resume-cap').innerText = (chargeTo >= 100 ? '自动' : resumeAt + '%');
    }
    if (document.getElementById('range-saver-cap')) {
      document.getElementById('range-saver-cap').value = saverBelow;
      document.getElementById('txt-saver-cap').innerText = saverBelow + '%';
    }
    const btn60 = document.getElementById('btn-preset-60');
    const btn80 = document.getElementById('btn-preset-80');
    if (btn60) btn60.className = 'p-2.5 rounded-xl text-left transition border ' + (chargeTo === 60 && resumeAt === 50 && !paused ? 'bg-emerald-500/15 border-emerald-500/60' : 'bg-slate-900/80 hover:bg-emerald-500/15 border-slate-800 hover:border-emerald-500/40');
    if (btn80) btn80.className = 'p-2.5 rounded-xl text-left transition border ' + (chargeTo === 80 && (resumeAt === 70 || rawResume === 0) && !paused ? 'bg-cyan-500/15 border-cyan-500/60' : 'bg-slate-900/80 hover:bg-cyan-500/15 border-slate-800 hover:border-cyan-500/40');

    const btnFull = document.getElementById('btn-full-once');
    if (btnFull) {
      btnFull.innerText = fullOnce ? '取消单次充满 (恢复限充)' : '出门前单次充满 100%';
      btnFull.className = 'py-2 px-3 rounded-xl font-medium transition border ' + (fullOnce ? 'bg-emerald-500/20 border-emerald-500/50 text-emerald-300' : 'bg-slate-800 hover:bg-slate-700 border-slate-700 text-slate-200');
    }
    const btnPause = document.getElementById('btn-pause-charge');
    if (btnPause) {
      btnPause.innerText = paused ? '恢复自动充电护航' : '手动停充直供';
      btnPause.className = 'py-2 px-3 rounded-xl font-medium transition border ' + (paused ? 'bg-amber-500/20 border-amber-500/50 text-amber-300' : 'bg-slate-800 hover:bg-slate-700 border-slate-700 text-slate-200');
    }
  }

  const cpuRes = await apiCall('cpu.get');
  if (cpuRes && cpuRes.ok && document.getElementById('cpu-governor')) {
    const cpuObj = cpuRes.cpu || cpuRes;
    const prof = cpuObj.profile || (cpuObj.governor === 'performance' ? 'performance' : (cpuObj.governor === 'powersave' ? 'saving' : 'balanced'));
    document.getElementById('cpu-governor').value = prof;
  }

  const zramRes = await apiCall('zram.get');
  if (zramRes && zramRes.ok && document.getElementById('zram-size')) {
    document.getElementById('zram-size').value = zramRes.enabled ? 'on' : 'off';
  }
}

function onChargeCapInput(val) {
  const ct = parseInt(val) || 80;
  document.getElementById('txt-charge-cap').innerText = ct + '%';
  const resSlider = document.getElementById('range-resume-cap');
  const resTxt = document.getElementById('txt-resume-cap');
  if (resSlider && resTxt) {
    if (ct >= 100) {
      resTxt.innerText = '自动';
    } else {
      let ra = parseInt(resSlider.value) || 70;
      if (ra >= ct - 2) {
        ra = Math.max(40, ct - 10);
        resSlider.value = ra;
      }
      resTxt.innerText = ra + '%';
    }
  }
}

async function applyPowerPreset(presetAction) {
  showToast('正在切换电池养护模式...');
  const res = await apiCall('power.control', { action: presetAction });
  if (res && res.ok) {
    showToast(res.msg || '电池养护模式已切换');
    await loadPowerTab();
  } else {
    showToast('操作失败: ' + ((res && res.error) || ''), false);
  }
}

async function toggleFullOnceCharge() {
  const st = window._powerConfState || {};
  const act = st.fullOnce ? 'cancel_full_once' : 'full_once';
  const res = await apiCall('power.control', { action: act });
  if (res && res.ok) {
    showToast(res.msg || '操作已生效');
    await loadPowerTab();
  } else {
    showToast('操作失败: ' + ((res && res.error) || ''), false);
  }
}

async function toggleManualChargePause() {
  const st = window._powerConfState || {};
  const act = st.paused ? 'resume' : 'pause';
  const res = await apiCall('power.control', { action: act });
  if (res && res.ok) {
    showToast(res.msg || '操作已生效');
    await loadPowerTab();
  } else {
    showToast('操作失败: ' + ((res && res.error) || ''), false);
  }
}

async function savePowerConfig() {
  const chargeTo = parseInt(document.getElementById('range-charge-cap').value) || 80;
  const resumeAt = chargeTo >= 100 ? 0 : (parseInt(document.getElementById('range-resume-cap').value) || Math.max(40, chargeTo - 10));
  const saverBelow = parseInt(document.getElementById('range-saver-cap').value) || 20;
  showToast('正在保存电池保护与滞回策略...');
  const res = await apiCall('power.control', { charge_to: chargeTo, resume_at: resumeAt, saver_below: saverBelow });
  if (res && res.ok) {
    showToast('电池保护与滞回阈值已生效');
    await loadPowerTab();
  } else {
    showToast('保存失败: ' + ((res && res.error) || ''), false);
  }
}

async function saveCpuConfig() {
  const profile = document.getElementById('cpu-governor').value || 'balanced';
  const zramState = document.getElementById('zram-size').value || 'on';
  showToast('正在应用 CPU 调频与 ZRAM 压缩策略...');
  const r1 = await apiCall('cpu.profile', { name: profile });
  const r2 = await apiCall('zram.set', { state: zramState });
  if ((r1 && r1.ok) || (r2 && r2.ok)) {
    showToast('CPU 与内存策略已生效');
  } else {
    showToast('保存失败: ' + ((r1 && r1.error) || (r2 && r2.error) || ''), false);
  }
}

async function checkBluetoothStatus() {
  try {
    const resp = await fetch('/api/bluetooth/status');
    const res = await resp.json();
    const badge = document.getElementById('bt-status-badge');
    const btn = document.getElementById('btn-bt-toggle');
    const detail = document.getElementById('bt-detail-txt');
    if (!badge || !btn) return;
    if (res.running) {
      badge.innerText = '已开启 (运行中)';
      badge.className = 'px-2.5 py-1 rounded text-xs font-semibold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30';
      btn.innerText = '关闭蓝牙 (省电推荐)';
      btn.className = 'px-4 py-2 bg-rose-600 hover:bg-rose-500 text-white rounded-xl text-xs font-semibold transition';
      if (detail) {
        detail.innerText = 'bluetooth.service & mu300-bluetooth 运行中';
        detail.className = 'text-emerald-400 font-mono';
      }
    } else {
      badge.innerText = '已关闭 (默认省电)';
      badge.className = 'px-2.5 py-1 rounded text-xs font-semibold bg-slate-800 text-slate-400 border border-slate-700';
      btn.innerText = '开启蓝牙服务';
      btn.className = 'px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-200 hover:text-white rounded-xl text-xs font-semibold border border-slate-700 transition';
      if (detail) {
        detail.innerText = 'bluetooth.service 已禁用';
        detail.className = 'text-slate-300 font-mono';
      }
    }
  } catch (e) {}
}

async function toggleBluetoothService() {
  showToast('正在切换蓝牙服务状态...');
  try {
    const resp = await fetch('/api/bluetooth/toggle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });
    const res = await resp.json();
    if (res.ok) {
      showToast(res.msg);
      checkBluetoothStatus();
    } else {
      showToast('切换蓝牙失败: ' + (res.error || ''), false);
    }
  } catch (e) {
    showToast('请求失败', false);
  }
}

async function loadLogStatus() {
  try {
    const resp = await fetch('/api/logs/status');
    if (!resp.ok) return;
    const res = await resp.json();
    if (!res.ok) return;

    if (res.disk) {
      const availGb = ((res.disk.avail_bytes || 0) / 1073741824).toFixed(2);
      const totalGb = ((res.disk.total_bytes || 0) / 1073741824).toFixed(2);
      const usedPct = res.disk.pct !== undefined ? res.disk.pct : (res.disk.used_pct || 0);
      const freePct = (100 - usedPct).toFixed(1);
      if (document.getElementById('log-disk-avail')) {
        document.getElementById('log-disk-avail').innerText = `${availGb} GB / ${totalGb} GB`;
      }
      if (document.getElementById('log-disk-pct')) {
        document.getElementById('log-disk-pct').innerText = `已用 ${usedPct}% (${freePct}% 空闲)`;
      }
    }
    if (res.logs) {
      if (document.getElementById('log-journal-size')) {
        document.getElementById('log-journal-size').innerText = formatBytes(res.logs.journal_bytes || 0);
      }
      if (document.getElementById('log-logcat-size')) {
        document.getElementById('log-logcat-size').innerText = formatBytes(res.logs.logcat_bytes || 0);
      }
      if (document.getElementById('log-apt-size')) {
        document.getElementById('log-apt-size').innerText = formatBytes(res.logs.apt_cache_bytes || 0);
      }
    }
    if (res.policy) {
      const p = res.policy;
      if (document.getElementById('log-last-clean')) {
        document.getElementById('log-last-clean').innerText = '上次巡检: ' + (p.last_clean_time ? p.last_clean_time.split(' ')[1] || p.last_clean_time : '已就绪');
      }
      if (document.getElementById('pol-journal-mb') && p.max_journal_mb) {
        document.getElementById('pol-journal-mb').value = String(p.max_journal_mb);
      }
      if (document.getElementById('pol-ret-days') && p.retention_days) {
        document.getElementById('pol-ret-days').value = String(p.retention_days);
      }
      if (document.getElementById('pol-mihomo-lvl') && p.mihomo_log_level) {
        document.getElementById('pol-mihomo-lvl').value = p.mihomo_log_level;
      }
      if (document.getElementById('pol-auto-clean') && p.auto_clean !== undefined) {
        document.getElementById('pol-auto-clean').checked = !!p.auto_clean;
      }
    }
  } catch (e) {}
}

async function cleanLogsNow(target = 'all') {
  showToast('正在执行日志与缓存清理...');
  try {
    const resp = await fetch('/api/logs/clean', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target })
    });
    const res = await resp.json();
    if (res.ok) {
      const freedTxt = res.freed_str || formatBytes(res.freed_bytes || 0);
      showToast(`清理完成！本次释放 ${freedTxt}`);
      loadLogStatus();
      fetchOverview();
    } else {
      showToast('清理失败: ' + (res.error || ''), false);
    }
  } catch (e) {
    showToast('清理请求异常', false);
  }
}

async function saveLogPolicy() {
  const payload = {
    auto_clean: document.getElementById('pol-auto-clean') ? document.getElementById('pol-auto-clean').checked : true,
    max_journal_mb: parseInt(document.getElementById('pol-journal-mb').value) || 8,
    retention_days: parseInt(document.getElementById('pol-ret-days').value) || 3,
    mihomo_log_level: document.getElementById('pol-mihomo-lvl').value || 'warning'
  };
  showToast('正在保存并下发日志守护策略...');
  try {
    const resp = await fetch('/api/logs/policy', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const res = await resp.json();
    if (res.ok) {
      showToast('日志守护策略已生效');
      loadLogStatus();
    } else {
      showToast('保存策略失败: ' + (res.error || ''), false);
    }
  } catch (e) {
    showToast('请求失败', false);
  }
}

async function exportBackup() {
  showToast('正在打包并下载系统配置备份 (.tar.gz)...');
  window.location.href = '/api/backup/download';
}

async function importBackupFile() {
  const defaultPath = '/tmp/';
  const file = prompt('请输入设备上待恢复的备份归档完整路径 (.tar.gz)：', defaultPath);
  if (!file) return;
  if (!confirm(`确定从备份【${file}】恢复配置吗？`)) return;
  showToast('正在恢复系统备份...');
  const res = await apiCall('backup.import', { file: file.trim(), force: true });
  if (res && res.ok) {
    showToast('配置恢复成功！');
  } else {
    showToast('恢复失败: ' + ((res && res.error) || ''), false);
  }
}

async function loadSysTab() {
  const res = await apiCall('system.get');
  if (res && res.ok) {
    if (res.hostname && document.getElementById('sys-hostname')) {
      document.getElementById('sys-hostname').value = res.hostname;
    }
  }
  const ttlRes = await apiCall('ttl.get');
  if (ttlRes && ttlRes.ok && document.getElementById('sys-ttl')) {
    const curTtl = ttlRes.ttl || ttlRes.value || '0';
    document.getElementById('sys-ttl').value = (curTtl === 'off' || !curTtl) ? '0' : String(curTtl);
  }
  const ledRes = await apiCall('led.get');
  if (ledRes && ledRes.ok && document.getElementById('sys-led')) {
    const l = ledRes.settings || ledRes.led || ledRes;
    if (l.network === false && l.wifi === false && l.battery === false) {
      document.getElementById('sys-led').value = 'off';
    } else if (Number(l.timeout) > 0) {
      document.getElementById('sys-led').value = 'breath';
    } else {
      document.getElementById('sys-led').value = 'on';
    }
  }
}

async function saveSysBasic() {
  const hn = document.getElementById('sys-hostname').value.trim();
  const ttl = document.getElementById('sys-ttl').value;
  const led = document.getElementById('sys-led').value;
  showToast('正在应用系统设定...');
  if (hn) await apiCall('system.hostname', { name: hn });
  if (ttl !== undefined) {
    await apiCall('ttl.set', { value: (ttl === '0' || ttl === '') ? 'off' : String(ttl) });
  }
  if (led) {
    const isOn = led !== 'off';
    await apiCall('led.set', {
      network: isOn,
      wifi: isOn,
      battery: isOn,
      timeout: led === 'breath' ? 60 : 0
    });
  }
  showToast('系统基础设定已保存');
  fetchOverview();
}

async function fetchSysLogs() {
  const box = document.getElementById('sys-log-box');
  box.innerText = '正在读取系统日志...';
  const res = await apiCall('logs', { lines: 200 });
  if (res && res.text) {
    box.innerText = res.text;
    box.scrollTop = box.scrollHeight;
  } else {
    box.innerText = '暂无系统日志';
  }
}

async function checkUpdate() {
  const btn = document.getElementById('btn-check-update') || (window.event ? window.event.target : null);
  if (btn) {
    btn.disabled = true;
    btn.innerText = '正在联网检查...';
  }
  showToast('正在向 GitHub 检测最新版本与更新日志...');
  try {
    await apiCall('update.check');
    setTimeout(async () => {
      const res = await apiCall('update.status');
      if (btn) {
        btn.disabled = false;
        btn.innerText = '检查新版更新';
      }
      if (res && res.status) {
        const cur = res.status.installed || 'v2026.10.26.1';
        const notice = res.status.notice || '';
        const elCur = document.getElementById('upd-cur-ver');
        if (elCur) elCur.innerText = cur;
        if (notice && notice !== cur) {
          showToast(`发现新版本 ${notice}（当前 ${cur}）`);
        } else {
          showToast(`检测完成：当前已是最新稳定版本 (${cur})`);
        }
      } else {
        showToast('检测完成：当前已是最新版本');
      }
    }, 2500);
  } catch (e) {
    if (btn) {
      btn.disabled = false;
      btn.innerText = '检查新版更新';
    }
    showToast('检查更新异常: ' + e.message, false);
  }
}

setInterval(renderTrafficChart, 1000);

window.addEventListener('DOMContentLoaded', () => {
  checkAuthStatus();
  renderTrafficChart();
});
