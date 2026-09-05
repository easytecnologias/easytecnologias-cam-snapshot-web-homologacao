document.addEventListener('DOMContentLoaded', () => {
  bindPlanningUi();
  lucide.createIcons();
  initNavGroups();
  hydrateResponsiveTables();
  const responsiveObserver = new MutationObserver(() => scheduleResponsiveHydration());
  responsiveObserver.observe(document.body, { childList: true, subtree: true });

  // Implantacao > OLT (cadastro). Os listeners vivem em js/deployOlt.js junto
  // do resto da tela, em vez de mais 4 linhas soltas neste arquivo -- que ja
  // tem 1.600 linhas de fiacao e foi o motivo de dividir o app.js.
  bindDeployOlt();
  bindMonitoring();

  // Dashboard drawer
  document.getElementById('dashDrawerClose')?.addEventListener('click', closeDashDrawer);
  document.getElementById('dashDrawerOverlay')?.addEventListener('click', closeDashDrawer);
  document.getElementById('closeMapLayerDetails')?.addEventListener('click', closeMapLayerDetails);
  document.getElementById('cancelMapLayerDetails')?.addEventListener('click', closeMapLayerDetails);

  // Login
  document.getElementById('loginForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = document.getElementById('loginBtn');
    btn.disabled = true;
    btn.textContent = 'Entrando';
    const user = document.getElementById('loginUser').value;
    const pass = document.getElementById('loginPassword').value;
    const result = await login(user, pass);
    if (result.ok) {
      showApp();
    } else {
      const err = document.getElementById('loginError');
      err.textContent = result.msg;
      err.hidden = false;
    }
    btn.disabled = false;
    btn.textContent = 'Entrar';
  });

  // Navegacao
  document.querySelectorAll('.nav-item[data-view]').forEach(btn => {
    btn.addEventListener('click', () => navigateTo(btn.dataset.view));
  });
  document.addEventListener('click', (e) => {
    const btn = e.target.closest?.('[data-dash-nav]');
    if (!btn) return;
    const view = btn.dataset.dashNav;
    if (view) navigateTo(view);
  });

  // Logout
  document.getElementById('logoutBtn').addEventListener('click', logout);
  document.getElementById('logoutBtnTop').addEventListener('click', logout);
  document.getElementById('btnStopActingAs')?.addEventListener('click', () => actAsTenant(''));
  document.getElementById('tenantModulesUnrestricted')?.addEventListener('change', _tenantModulesToggleUnrestricted);
  document.getElementById('btnSaveTenantModules')?.addEventListener('click', saveTenantModules);
  document.getElementById('btnSaveTenantEdit')?.addEventListener('click', saveTenantEdit);
  document.getElementById('btnCancelTenantEdit')?.addEventListener('click', closeTenantEditModal);
  document.getElementById('btnCloseTenantEdit')?.addEventListener('click', closeTenantEditModal);
  document.getElementById('btnSaveTenantZabbix')?.addEventListener('click', saveTenantZabbixAccess);
  document.getElementById('btnCancelTenantZabbix')?.addEventListener('click', closeTenantZabbixModal);
  document.getElementById('btnCloseTenantZabbix')?.addEventListener('click', closeTenantZabbixModal);
  document.getElementById('settingsTenantsBody')?.addEventListener('click', handleTenantActionClick);
  document.getElementById('btnSaveEditUser')?.addEventListener('click', saveEditUser);

  // Painel do Dono <-> Painel operacional (so admin de plataforma ve esses botoes)
  document.getElementById('btnEnterOperation')?.addEventListener('click', () => {
    setShellMode('client');
    navigateTo('dashboard');
  });
  document.getElementById('btnBackToOwnerPanel')?.addEventListener('click', () => {
    setShellMode('owner');
    navigateTo('owner-overview');
  });
  document.getElementById('btnOwnerOverviewRefresh')?.addEventListener('click', loadOwnerOverview);
  document.getElementById('btnOwnerClientsRefresh')?.addEventListener('click', loadOwnerClients);
  document.getElementById('btnOwnerAuditRefresh')?.addEventListener('click', loadOwnerAudit);

  // Configuracoes SaaS
  document.getElementById('btnSettingsRefresh')?.addEventListener('click', loadSettings);
  document.getElementById('btnCreateTenant')?.addEventListener('click', createTenantFromSettings);
  document.getElementById('btnCreateUser')?.addEventListener('click', createUserFromSettings);
  document.getElementById('btnTelegramSave')?.addEventListener('click', () => saveTelegramSettings().catch(error => showToast(error.message, true)));
  document.getElementById('btnTelegramTest')?.addEventListener('click', () => testTelegramSettings().catch(error => showToast(error.message, true)));
  document.querySelectorAll('[data-settings-tab]').forEach(button => {
    button.addEventListener('click', () => activateSettingsTab(button.dataset.settingsTab));
  });

  // Profile dropdown
  document.getElementById('profileMenu').addEventListener('click', (e) => {
    e.stopPropagation();
    document.getElementById('profileDropdown').classList.toggle('open');
  });
  document.addEventListener('click', () => {
    document.getElementById('profileDropdown').classList.remove('open');
  });

  // Mobile menu
  document.getElementById('menuBtn').addEventListener('click', openSidebar);
  document.getElementById('mobileBackdrop').addEventListener('click', closeSidebar);

  // Dashboard e varredura contextual
  document.getElementById('btnDashboardRefresh')?.addEventListener('click', async (event) => {
    const btn = event.currentTarget;
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-circle"></i> Atualizando';
    lucide.createIcons();
    try {
      await refreshDashboardLiveCameraStatus();
    } finally {
      btn.disabled = false;
      btn.innerHTML = '<i data-lucide="refresh-cw"></i> Atualizar';
      lucide.createIcons();
    }
  });
  document.getElementById('closeScanModal').addEventListener('click', closeScanModal);
  document.getElementById('cancelScan').addEventListener('click', closeScanModal);
  document.getElementById('startScan').addEventListener('click', startScan);
  document.getElementById('scanOrigin')?.addEventListener('change', updateScanOriginUi);
  document.getElementById('scanLocal')?.addEventListener('change', () => refreshScanConnectors().finally(updateScanOriginUi));
  document.getElementById('scanConnector')?.addEventListener('change', updateScanOriginUi);
  document.getElementById('scanMode')?.addEventListener('change', () => {
    updateScanOriginUi();
    updateScanEnrichDefaults();
  });

  // Conectores SaaS
  document.getElementById('btnConnectorRefresh')?.addEventListener('click', async () => {
    const btn = document.getElementById('btnConnectorRefresh');
    const oldHtml = btn?.innerHTML;
    if (btn) {
      btn.disabled = true;
      btn.innerHTML = '<i data-lucide="loader-circle"></i> Atualizando';
      lucide.createIcons();
    }
    try {
      clearApiJsonCache('/api/connectors');
      await loadConnectors(true);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = oldHtml || '<i data-lucide="refresh-cw"></i> Atualizar';
        lucide.createIcons();
      }
    }
  });
  document.getElementById('btnConnectorNew')?.addEventListener('click', openConnectorCreateModal);
  document.getElementById('btnConnectorModalClose')?.addEventListener('click', closeConnectorCreateModal);
  document.getElementById('btnConnectorModalCancel')?.addEventListener('click', closeConnectorCreateModal);
  document.getElementById('btnCreateConnector')?.addEventListener('click', createConnectorFromForm);
  document.getElementById('btnDownloadCreatedAgent')?.addEventListener('click', () => downloadConnectorAgent(_lastCreatedConnectorId));
  document.getElementById('btnSendPingJob')?.addEventListener('click', sendConnectorPingJob);
  document.getElementById('btnSendLanInventoryJob')?.addEventListener('click', sendConnectorLanInventoryJob);
  document.getElementById('connJobConnector')?.addEventListener('change', (e) => loadConnectorJobs(e.target.value));
  document.getElementById('closeConnectorVpnModal')?.addEventListener('click', closeConnectorVpnModal);
  document.getElementById('cancelConnectorVpnModal')?.addEventListener('click', closeConnectorVpnModal);
  document.getElementById('confirmConnectorVpnModal')?.addEventListener('click', submitConnectorVpnModal);
  document.querySelectorAll('input[name="connectorVpnLanMode"]').forEach(el => el.addEventListener('change', updateConnectorVpnLanMode));
  document.getElementById('closeRuijieVpnModal')?.addEventListener('click', closeRuijieVpnModal);
  document.getElementById('cancelRuijieVpnModal')?.addEventListener('click', closeRuijieVpnModal);
  document.getElementById('confirmRuijieVpnModal')?.addEventListener('click', submitRuijieVpnModal);
  document.getElementById('connectorsTable')?.addEventListener('click', (e) => {
    const trigger = e.target.closest('[data-conn-menu]');
    if (trigger) openConnectorActionMenu(e, trigger.dataset.connMenu, trigger);
  });
  document.addEventListener('click', closeConnectorActionMenu);
  window.addEventListener('resize', closeConnectorActionMenu);
  window.addEventListener('scroll', closeConnectorActionMenu, true);

  // Ferramentas de rede
  document.getElementById('netToolForm')?.addEventListener('submit', runNetTool);
  document.getElementById('netToolOrigin')?.addEventListener('change', updateNetToolFormState);
  document.getElementById('netToolTest')?.addEventListener('change', updateNetToolFormState);
  document.getElementById('btnClearNetToolLog')?.addEventListener('click', () => netToolSetLog('Nenhum teste executado ainda.', 'Aguardando teste.'));

  // Implantacao - ONU (pagina dedicada)
  document.getElementById('btnOnuDiscover')?.addEventListener('click', onuDiscover);
  document.getElementById('btnOnuAdd')?.addEventListener('click', onuAdd);
  document.getElementById('onuAddTerminal')?.addEventListener('change', onuUpdateTerminalUI);
  document.getElementById('btnOnuAddVlanRow')?.addEventListener('click', onuAddVlanRow);
  document.getElementById('btnOnuAddPortRowEpon')?.addEventListener('click', onuAddPortRowEpon);
  document.getElementById('btnOnuQuery')?.addEventListener('click', onuQuery);
  document.getElementById('btnOnuReboot')?.addEventListener('click', onuReboot);
  document.getElementById('btnOnuDelete')?.addEventListener('click', onuDelete);
  document.getElementById('btnOnuClear')?.addEventListener('click', onuClear);
  document.getElementById('btnOnuHistoryRefresh')?.addEventListener('click', loadOnuHistory);
  document.getElementById('confirmOnuDelete')?.addEventListener('click', onuConfirmDelete);
  document.getElementById('cancelOnuDelete')?.addEventListener('click', closeOnuDeleteModal);
  document.getElementById('closeOnuDeleteModalBtn')?.addEventListener('click', closeOnuDeleteModal);

  // Implantacao - Gravadores
  document.querySelectorAll('.deployment-inventory-mode').forEach(select => {
    select.addEventListener('change', () => deploymentSetPreferredInventoryMode(select.value));
  });
  document.getElementById('deployStandaloneRecorderConnector')?.addEventListener('change', () => {
    deployStandaloneRecorderClearRecorderFields({ keepConnector: true });
    deployStandaloneRecorderRenderOltsForOrigin();
    deployStandaloneRecorderRenderConnectorStatus();
  });
  document.getElementById('btnDeployStandaloneRecorderOpenEntryModal')?.addEventListener('click', openDeployStandaloneRecorderEntryModal);
  document.getElementById('btnDeployStandaloneRecorderOpenModal')?.addEventListener('click', () => openDeployStandaloneRecorderModal('create'));
  document.getElementById('btnDeployStandaloneRecorderCloseModal')?.addEventListener('click', closeDeployStandaloneRecorderModal);
  document.getElementById('btnDeployStandaloneRecorderCancelModal')?.addEventListener('click', closeDeployStandaloneRecorderModal);
  document.getElementById('btnDeployStandaloneRecorderLoginModal')?.addEventListener('click', deployStandaloneRecorderLogin);
  document.getElementById('btnDeployStandaloneRecorderClear')?.addEventListener('click', deployStandaloneRecorderClear);
  document.getElementById('btnDeployStandaloneRecorderReloadSaved')?.addEventListener('click', deployStandaloneRecorderLoadSaved);
  document.getElementById('deployStandaloneRecorderSavedSelect')?.addEventListener('change', event => {
    const key = event.target?.value || '';
    if (key) deployStandaloneRecorderUseSaved(key);
  });
  document.getElementById('btnDeployStandaloneRecorderOpenWeb')?.addEventListener('click', deployStandaloneRecorderOpenWeb);
  document.getElementById('btnDeployStandaloneRecorderRefreshChannels')?.addEventListener('click', deployStandaloneRecorderLogin);
  document.getElementById('btnDeployStandaloneRecorderPlayback')?.addEventListener('click', deployStandaloneRecorderPlayback);
  document.getElementById('btnDeployStandaloneRecorderSetNtp')?.addEventListener('click', deployStandaloneRecorderSetNtp);
  document.getElementById('btnDeployStandaloneRecorderReboot')?.addEventListener('click', deployStandaloneRecorderReboot);
  document.getElementById('btnDeployStandaloneRecorderFicha')?.addEventListener('click', deployStandaloneRecorderFicha);
  document.getElementById('deployRecorderChannelDrawerBackdrop')?.addEventListener('click', () => {
    _deployRecorderSelectedChannel = 0;
    deployRenderStandaloneRecorderChannels();
  });
  document.getElementById('deployRecorderChannelSearch')?.addEventListener('input', deployRenderStandaloneRecorderChannels);
  document.getElementById('deployRecorderChannelFilter')?.addEventListener('change', deployRenderStandaloneRecorderChannels);
  document.getElementById('viewDeployRecorder')?.addEventListener('click', event => {
    const savedRecorder = event.target.closest('[data-recorder-saved-key]');
    if (savedRecorder) {
      deployStandaloneRecorderUseSaved(savedRecorder.dataset.recorderSavedKey || '');
      return;
    }
    const channelCard = event.target.closest('[data-recorder-channel]');
    if (channelCard) {
      _deployRecorderSelectedChannel = Number(channelCard.dataset.recorderChannel || 0);
      deployRenderStandaloneRecorderChannels();
      return;
    }
    const channelAction = event.target.closest('[data-recorder-channel-action]')?.dataset.recorderChannelAction;
    if (channelAction) {
      const item = deployStandaloneRecorderChannelsFromProbe(_deployStandaloneRecorderProbe).find(row => Number(row.channel) === _deployRecorderSelectedChannel);
      if (channelAction === 'close') { _deployRecorderSelectedChannel = 0; deployRenderStandaloneRecorderChannels(); return; }
      if (channelAction === 'refresh') { deployStandaloneRecorderLogin(); return; }
      if (channelAction === 'web' && item?.camera_ip) { window.open(`${API_BASE}/api/maintenance/web/${encodeURIComponent(item.camera_ip)}/`, '_blank', 'noopener'); return; }
      if (channelAction === 'ping' && item?.camera_ip) { openPingTerminal(item.camera_ip); return; }
      if (channelAction === 'delete' && item?.used) { deployStandaloneRecorderDeleteChannel(item); return; }
      if (channelAction === 'add') { navigateTo('deploy-new'); return; }
    }
    const action = event.target.closest('[data-recorder-overview-action]')?.dataset.recorderOverviewAction;
    if (action === 'refresh') { deployStandaloneRecorderLogin(); return; }
    if (action === 'channels') { deployStandaloneRecorderSelectConfigTab('channels'); return; }
    if (action === 'save') { deployStandaloneRecorderSave(); return; }
    const tab = event.target.closest('.recorder-config-tab');
    if (!tab) return;
    deployStandaloneRecorderSelectConfigTab(tab.dataset.recorderConfigTab || 'overview');
  });
  ['deployStandaloneRecorderHost', 'deployStandaloneRecorderType', 'deployStandaloneRecorderPort', 'deployStandaloneRecorderChannelTotal'].forEach(id => {
    document.getElementById(id)?.addEventListener('input', () => {
      _deployStandaloneRecorderProbe = null;
      deployStandaloneRecorderRenderProbe(null);
      deployStandaloneRecorderSetResult('Entre no gravador para validar modelo, serial e canais.');
      deployStandaloneRecorderSetQuickResult('Entre no gravador para liberar as configuracoes rapidas.');
    });
  });

  // Implantacao
  document.getElementById('btnDeployClear')?.addEventListener('click', deployClear);
  document.getElementById('deployForm')?.addEventListener('submit', deployCommitCamera);
  document.getElementById('btnDeploySave')?.addEventListener('click', deploySaveDraft);
  document.getElementById('btnDeployLookupMac')?.addEventListener('click', deployLookupMac);
  document.getElementById('btnDeployPullCamera')?.addEventListener('click', deployPullCameraInfo);
  document.getElementById('btnDeployCheckNewIp')?.addEventListener('click', deployCheckNewIp);
  document.getElementById('btnDeploySaveCameraInventory')?.addEventListener('click', deploySaveCameraInventory);
  document.getElementById('btnDeployRecorderLogin')?.addEventListener('click', deployRecorderLogin);
  document.getElementById('btnDeployRecorderAddCamera')?.addEventListener('click', deployRecorderAddCamera);
  document.getElementById('deployRecorderType')?.addEventListener('change', deployLoadAvailableRecorders);
  document.getElementById('deployRecorderHost')?.addEventListener('change', deployApplySelectedRecorder);
  document.getElementById('deployRecorderUser')?.addEventListener('input', deployResetRecorderLogin);
  document.getElementById('deployRecorderPassword')?.addEventListener('input', deployResetRecorderLogin);
  document.getElementById('deployRecorderChannelButton')?.addEventListener('click', deployToggleRecorderChannelDropdown);
  document.getElementById('deployRecorderChannelGrid')?.addEventListener('click', (ev) => {
    const btn = ev.target.closest?.('.deploy-channel-pill');
    if (btn) deploySelectRecorderChannel(btn.dataset.channel);
  });
  document.addEventListener('click', (ev) => {
    const dropdown = document.getElementById('deployRecorderChannelDropdown');
    if (!dropdown || dropdown.contains(ev.target)) return;
    document.getElementById('deployRecorderChannelGrid')?.classList.add('hidden');
  });
  document.getElementById('deployConnector')?.addEventListener('change', () => {
    deployRenderOltContextForOrigin();
    deployApplyOltContext();
    deployApplyOriginFields();
    deployRenderConnectorStatus();
    deployRenderSummary();
    deployUpdateStepLocks({ autoAdvance: true });
    deployLoadAvailableRecorders();
  });
  document.getElementById('deployStandaloneRecorderOlt')?.addEventListener('change', deployStandaloneRecorderApplyOlt);
  document.getElementById('deployOltContext')?.addEventListener('change', deployApplyOltContext);
  document.getElementById('deploySite')?.addEventListener('input', () => {
    deployRenderSummary();
    deployUpdateStepLocks({ autoAdvance: true });
    deployScheduleAvailableRecorders();
  });
  document.getElementById('deployCameraTitle')?.addEventListener('input', () => {
    const recTitle = document.getElementById('deployRecorderTitle');
    if (recTitle && !recTitle.value) recTitle.value = document.getElementById('deployCameraTitle')?.value || '';
    deployRenderSummary();
    deployUpdateStepLocks({ autoAdvance: true });
  });
  document.getElementById('deployForm')?.addEventListener('input', deployRenderSummary);
  document.getElementById('deployForm')?.addEventListener('change', deployRenderSummary);

  // ImgBB settings
  document.getElementById('btnImgbbSettings')?.addEventListener('click', openImgbbModal);
  document.getElementById('btnNvrImgbbSettings')?.addEventListener('click', openImgbbModal);
  document.getElementById('closeImgbbModal')?.addEventListener('click', () => document.getElementById('modalImgbb').classList.add('hidden'));
  document.getElementById('cancelImgbbModal')?.addEventListener('click', () => document.getElementById('modalImgbb').classList.add('hidden'));

  document.getElementById('btnTestImgbb')?.addEventListener('click', async () => {
    const key = document.getElementById('imgbbApiKey').value.trim();
    if (!key) { showToast('Informe a API key', true); return; }
    const res = await api('/api/settings/imgbb/test', { method: 'POST', body: JSON.stringify({ api_key: key }) });
    const result = document.getElementById('imgbbTestResult');
    result.style.display = 'block';
    if (res?.ok) {
      result.style.background = 'var(--primary-soft)';
      result.style.color = 'var(--primary)';
      result.textContent = ' API key valida! Conexao com ImgBB funcionando.';
    } else {
      const err = await res?.json().catch(() => ({}));
      result.style.background = 'var(--danger-soft)';
      result.style.color = 'var(--danger)';
      result.textContent = ' ' + (err?.detail || 'API key invalida ou erro de conexao.');
    }
  });

  document.getElementById('saveImgbbModal')?.addEventListener('click', async () => {
    const key = document.getElementById('imgbbApiKey').value.trim();
    if (!key) { showToast('Informe a API key', true); return; }
    const res = await api('/api/settings/imgbb', { method: 'POST', body: JSON.stringify({ api_key: key }) });
    if (!res?.ok) {
      const err = await res?.json().catch(() => ({}));
      const el = document.getElementById('imgbbErro');
      el.textContent = err?.detail || 'Erro ao salvar.'; el.hidden = false; return;
    }
    document.getElementById('modalImgbb').classList.add('hidden');
    showToast('API key ImgBB salva!');
  });

  // Botoes individuais de tarefa no modal de scan
  document.querySelectorAll('.scan-task-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const base = _scanPayloadBase();
      if (!base) return;
      const task = btn.dataset.task;
      const taskMap = {
        discover:   { },
        olt_enrich: { olt_enrich: true },
        switch_enrich: { switch_enrich: true },
        snapshot:   { snapshot: true },
        imgbb:      { imgbb: true },
        ia:         { ia: true },
      };
      _runWsScan({ ...base, ...taskMap[task] });
    });
  });

  // Tabs de visao do inventario OLT
  document.querySelectorAll('.inv-view-tab').forEach(btn => {
    btn.addEventListener('click', () => setInvOltView(btn.dataset.view));
  });

  // Filtros inventario OLT
  document.getElementById('searchInvOlt')?.addEventListener('input', applyInvOltFilters);
  document.getElementById('filterStatusOlt')?.addEventListener('change', applyInvOltFilters);
  document.getElementById('filterSiteOlt')?.addEventListener('change', () => {
    applyInvOltFilters();
    refreshScanConnectors().finally(updateScanOriginUi);
  });
  document.getElementById('btnOltClearFilter')?.addEventListener('click', () => {
    document.getElementById('searchInvOlt').value = '';
    document.getElementById('filterStatusOlt').value = '';
    document.getElementById('filterSiteOlt').value = '';
    applyInvOltFilters();
  });

  // Painel camera
  document.getElementById('btnCloseCamPanel')?.addEventListener('click', closeCamPanel);
  document.getElementById('camPanelBackdrop')?.addEventListener('click', closeCamPanel);
  document.getElementById('cpBtnAtualizar')?.addEventListener('click', () => camAction('atualizar'));
  document.getElementById('cpBtnRenomear')?.addEventListener('click', () => camAction('renomear'));
  document.getElementById('cpBtnTrocarIp')?.addEventListener('click', () => camAction('trocar-ip'));
  document.getElementById('cpBtnTrocarSenha')?.addEventListener('click', () => camAction('trocar-senha'));
  document.getElementById('cpBtnDataHora')?.addEventListener('click', () => camAction('data-hora'));
  document.getElementById('cpBtnReboot')?.addEventListener('click', () => camAction('reboot'));
  document.getElementById('cpBtnWeb')?.addEventListener('click', () => camAction('web'));
  document.getElementById('cpBtnMapa')?.addEventListener('click', () => focusCameraOnMap(_invOltActive));
  document.getElementById('cpBtnPing')?.addEventListener('click', startPing);
  document.getElementById('cpSnapshotWrap')?.addEventListener('click', openCamPanelLive);
  document.getElementById('cpBtnLive')?.addEventListener('click', (event) => {
    event.stopPropagation();
    openCamPanelLive();
  });
  document.getElementById('cpLiveStart')?.addEventListener('click', startCamPanelLive);
  document.getElementById('cpLiveClose')?.addEventListener('click', (event) => {
    event.stopPropagation();
    closeCamPanelLive();
  });
  document.getElementById('cpLivePass')?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') startCamPanelLive();
  });
  document.getElementById('cpLivePassToggle')?.addEventListener('click', (event) => {
    event.stopPropagation();
    toggleCamLivePassword();
  });
  document.getElementById('cpLiveFullscreen')?.addEventListener('click', (event) => {
    event.stopPropagation();
    fullscreenCamPanelLive();
  });
  document.getElementById('cpLiveQuality')?.addEventListener('click', (event) => {
    event.stopPropagation();
    toggleCamPanelLiveQuality();
  });
  document.getElementById('cpLiveChangeLogin')?.addEventListener('click', (event) => {
    event.stopPropagation();
    changeCamLiveCredential();
  });
  document.addEventListener('fullscreenchange', () => {
    const btn = document.getElementById('cpLiveFullscreen');
    if (!btn) return;
    const expanded = Boolean(document.fullscreenElement) || document.getElementById('cpInlineLive')?.classList.contains('mobile-fullscreen');
    btn.innerHTML = `<i data-lucide="${expanded ? 'minimize-2' : 'maximize-2'}"></i>`;
    btn.title = expanded ? 'Reduzir video' : 'Ampliar video';
    lucide.createIcons();
  });
  document.getElementById('closeCamAuthAction')?.addEventListener('click', closeCamAuthAction);
  document.getElementById('cancelCamAuthAction')?.addEventListener('click', closeCamAuthAction);
  document.getElementById('confirmCamAuthAction')?.addEventListener('click', runCamAuthAction);
  document.getElementById('camAuthPass')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') runCamAuthAction();
  });
  document.getElementById('pingTermStop')?.addEventListener('click', () => {
    stopPing();
    pingLine(' parado ', 'info');
  });
  document.getElementById('pingTermRestart')?.addEventListener('click', () => {
    pingLine(' reiniciando ', 'info');
    runPing();
  });
  document.getElementById('pingTermClear')?.addEventListener('click', () => {
    document.getElementById('pingTermBody').innerHTML = '';
    document.getElementById('pingTermStats').textContent = '';
    _pingCount = 0; _pingOk = 0; _pingFail = 0;
  });
  document.getElementById('pingTermClose')?.addEventListener('click', closePingTerminal);
  document.getElementById('cpBtnLimpar')?.addEventListener('click', () => camAction('limpar'));

  // Rodape inventario OLT
  document.getElementById('btnOltBackup')?.addEventListener('click', () => window.open(`${API_BASE}/api/backup/export`, '_blank'));
  document.getElementById('btnOltPdf')?.addEventListener('click', (event) => runInvOltReport(event.currentTarget));
  document.getElementById('btnOltImgbb')?.addEventListener('click', () => {
    const checked = [...document.querySelectorAll('.chk-olt:checked')];
    const ips = checked.map(c => c.value);
    const keys = checked.map(c => c.dataset.key || '').filter(Boolean);
    const allCams = ips.length === 0
      ? _invOltAll_get()
      : _invOltAll_get().filter(c => keys.includes(_camKey(c)) || ips.includes(c.ip));

    const list = document.getElementById('imgbbUploadList');
    const desc = document.getElementById('imgbbUploadDesc');
    desc.textContent = ips.length
      ? `${ips.length} camera(s) selecionada(s) serao enviadas ao ImgBB.`
      : `Nenhuma camera selecionada. Serao enviadas TODAS (${_invOltAll_get().length} cameras).`;

    list.innerHTML = allCams.slice(0, 20).map(c => `
      <div style="display:flex;align-items:center;justify-content:space-between;padding:6px 10px;border:1px solid var(--border);border-radius:6px;font-size:12px;background:var(--surface-soft)">
        <span class="monospace">${esc(c.ip)}</span>
        <span style="color:var(--muted)">${esc(c.titulo || '')}</span>
        <span>${cameraImgbbUrl(c) ? '<span style="color:var(--primary);font-weight:600"> up</span>' : '<span style="color:var(--danger);font-weight:600"> down</span>'}</span>
      </div>`).join('') + (allCams.length > 20 ? `<p style="font-size:12px;color:var(--muted);text-align:center;margin:4px 0">+ ${allCams.length - 20} mais</p>` : '');

    document.getElementById('imgbbUploadProgress').style.display = 'none';
    document.getElementById('imgbbUploadBar').style.width = '0%';
    document.getElementById('imgbbUploadMsg').textContent = '';
    document.getElementById('modalImgbbUpload').classList.remove('hidden');
    lucide.createIcons();

    // Armazena IPs para o confirm
    document.getElementById('confirmImgbbUpload').dataset.ips = JSON.stringify(ips);
    document.getElementById('confirmImgbbUpload').dataset.keys = JSON.stringify(keys);
  });

  document.getElementById('closeImgbbUpload')?.addEventListener('click', () =>
    document.getElementById('modalImgbbUpload').classList.add('hidden'));
  document.getElementById('cancelImgbbUpload')?.addEventListener('click', () =>
    document.getElementById('modalImgbbUpload').classList.add('hidden'));

  document.getElementById('confirmImgbbUpload')?.addEventListener('click', async () => {
    const ips = JSON.parse(document.getElementById('confirmImgbbUpload').dataset.ips || '[]');
    const keys = JSON.parse(document.getElementById('confirmImgbbUpload').dataset.keys || '[]');
    const progress = document.getElementById('imgbbUploadProgress');
    const bar = document.getElementById('imgbbUploadBar');
    const msg = document.getElementById('imgbbUploadMsg');
    const btn = document.getElementById('confirmImgbbUpload');

    progress.style.display = 'block';
    bar.style.width = '30%';
    msg.textContent = 'Enviando fotos';
    msg.style.color = 'var(--muted)';
    btn.disabled = true;

    try {
      const allRows = _invOltAll_get();
      const selectedRows = ips.length
        ? allRows.filter(c => keys.includes(_camKey(c)) || ips.includes(c.ip))
        : allRows;
      const uploadIps = selectedRows.map(c => c.ip).filter(Boolean);
      const uploadKeys = selectedRows.map(c => _camKey(c)).filter(Boolean);
      const chunkSize = 1;
      const chunks = [];
      for (let i = 0; i < uploadIps.length; i += chunkSize) {
        chunks.push({
          ips: uploadIps.slice(i, i + chunkSize),
          keys: uploadKeys.slice(i, i + chunkSize),
        });
      }
      if (!chunks.length) throw new Error('Nenhuma camera selecionada para upload.');

      let uploadedTotal = 0;
      let processedTotal = 0;
      let skippedNoSnapshot = 0;
      let failedTotal = 0;
      let lastError = '';
      let updatedRows = [];

      for (let i = 0; i < chunks.length; i += 1) {
        const part = chunks[i];
        const pct = Math.max(8, Math.round((i / chunks.length) * 92));
        bar.style.width = `${pct}%`;
        msg.textContent = `Enviando lote ${i + 1}/${chunks.length} (${part.ips.length} camera(s))`;

        const payload = { mode: _invOltView || 'olt', ips: part.ips, keys: part.keys };
        const res = await api('/api/inventory/imgbb/upload', { method: 'POST', body: JSON.stringify(payload) });
        if (!res) throw new Error('Erro ao enviar ao ImgBB.');
        const text = await res.text();
        let data = {};
        try {
          data = text ? JSON.parse(text) : {};
        } catch {
          const clean = String(text || '').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 180);
          throw new Error(clean ? `Servidor retornou resposta inesperada: ${clean}` : 'Erro ao enviar ao ImgBB.');
        }

        const skippedFromApi = Number(data?.skipped_no_snapshot || 0);
        if (skippedFromApi > 0) {
          skippedNoSnapshot += skippedFromApi;
          lastError = data?.error ? String(data.error) : lastError;
          processedTotal += Number(data?.processed || part.ips.length || 0);
          continue;
        }

        if (data?.ok === false && Number(data?.uploaded || 0) === 0) {
          const err = String(data?.error || 'Nenhuma foto foi enviada ao ImgBB.');
          const errLower = err.toLowerCase();
          if (errLower.includes('rate limit')) {
            throw new Error(`ImgBB atingiu limite de envio. Enviadas: ${uploadedTotal}. Tente novamente mais tarde.`);
          }
          if (errLower.includes('nenhum snapshot local')) {
            skippedNoSnapshot += Number(data?.processed || part.ips.length || 1);
            lastError = err;
            continue;
          }
          failedTotal += Number(data?.processed || part.ips.length || 1);
          lastError = err;
          continue;
        }

        uploadedTotal += Number(data?.uploaded || 0);
        processedTotal += Number(data?.processed || part.ips.length || 0);
        if (data?.error) lastError = String(data.error);
        if (Array.isArray(data?.inventory) && data.inventory.length) updatedRows = data.inventory;
        if (i < chunks.length - 1) {
          await new Promise(resolve => setTimeout(resolve, 2500));
        }
      }

      bar.style.width = '100%';
      const sentIps = new Set(uploadIps);
      const sentKeys = new Set(uploadKeys);

      if (updatedRows.length) {
        const byKey = {};
        updatedRows.forEach(r => { byKey[_camKey(r)] = r; });
        Object.keys(_invCam).forEach(mode => {
          _invCam[mode] = (_invCam[mode] || []).map(c => {
            const cKey = _camKey(c);
            const u = byKey[cKey];
            if (!u || (!sentKeys.has(cKey) && !sentIps.has(c.ip))) return c;
            const imgbbUrl = cameraImgbbUrl(u);
            return {
              ...c,
              ...u,
              imgbb_url: imgbbUrl || c.imgbb_url || '',
              imgbb_thumb_url: u.imgbb_thumb_url || u.thumbnail_url || c.imgbb_thumb_url || imgbbUrl || '',
              imgbb_status: imgbbUrl ? 'up' : (u.imgbb_status || c.imgbb_status || ''),
            };
          });
          _camSessionSave(mode, _invCam[mode]);
        });
      }

      _camSessionClear();
      await _loadCamForMode(_invOltView);
      populateCamSiteFilter();
      applyInvOltFilters();

      const noisyNoSnapshot = lastError && lastError.toLowerCase().includes('nenhum snapshot local');
      const suffix = lastError && !(uploadedTotal > 0 && noisyNoSnapshot) ? ` (${lastError})` : '';
      msg.textContent = uploadedTotal > 0
        ? ` ${uploadedTotal} foto(s) enviada(s). ${skippedNoSnapshot} sem snapshot local, ${failedTotal} falharam.${suffix}`
        : ` Nenhuma foto enviada. ${skippedNoSnapshot} sem snapshot local, ${failedTotal} falharam.${suffix}`;
      msg.style.color = uploadedTotal > 0 ? 'var(--primary)' : 'var(--danger)';
      showToast(uploadedTotal > 0 ? `${uploadedTotal} foto(s) enviada(s) ao ImgBB.` : 'Nenhuma foto enviada ao ImgBB.', uploadedTotal === 0);
      if (uploadedTotal > 0) {
        setTimeout(() => {
          document.getElementById('modalImgbbUpload').classList.add('hidden');
        }, 1400);
      }
    } catch (err) {
      bar.style.width = '100%';
      msg.textContent = ' ' + (err?.message || 'Erro ao enviar.');
      msg.style.color = 'var(--danger)';
      showToast(err?.message || 'Erro ao enviar ImgBB.', true);
    } finally {
      btn.disabled = false;
    }
  });
  document.getElementById('btnOltPingSelected')?.addEventListener('click', async () => {
    const ips = [...document.querySelectorAll('.chk-olt:checked')].map(c => c.value);
    if (!ips.length) { showToast('Selecione ao menos uma camera', true); return; }
    showToast(`Pingando ${ips.length} camera(s)`);
    const res = await api('/api/cameras/ping_many', {
      method: 'POST',
      body: JSON.stringify({ ips, force: 1, timeout: 3 }),
    });
    const data = await res?.json().catch(() => ({}));
    if (!res?.ok || data?.ok === false) {
      showToast(data?.detail || data?.error || 'Erro ao executar ping.', true);
      return;
    }
    showToast(`Ping concluido: ${data.online || 0} responderam, ${data.offline || 0} sem resposta. Status da tabela nao foi alterado.`);
  });

  // Editar selecionados (um ou varios)
  document.getElementById('btnOltEditar')?.addEventListener('click', () => {
    const keys = [...document.querySelectorAll('.chk-olt:checked')].map(c => c.dataset.key || `IP:${c.value}`);
    if (!keys.length) { showToast('Selecione ao menos uma camera para editar', true); return; }
    const cams = keys.map(key => _invOltAll_get().find(c => _camKey(c) === key)).filter(Boolean);
    openEditCamModal(cams);
  });

  // Apagar selecionados
  document.getElementById('btnOltDeleteSelected')?.addEventListener('click', async () => {
    const checked = [...document.querySelectorAll('.chk-olt:checked')];
    const ips = checked.map(c => c.value);
    const keys = checked.map(c => c.dataset.key || `IP:${c.value}`);
    const site = document.getElementById('filterSiteOlt')?.value || '';
    if (!ips.length) { showToast('Selecione ao menos uma camera', true); return; }
    if (!await showConfirm({ title: 'Remover cameras', msg: `Remover ${ips.length} camera(s) do inventario? Nao volta sozinho mesmo se a OLT continuar vendo o mesmo IP na rede.`, label: 'Remover' })) return;
    const res = await api('/api/inventory/delete', {
      method: 'POST',
      body: JSON.stringify({ ips, keys, mode: _invOltView || 'olt', site, permanent: true }),
    });
    const data = await res?.json().catch(() => ({}));
    if (!res?.ok || data?.ok === false) {
      showToast(data?.detail || data?.error || 'NAo foi possAvel remover as cAmeras.', true);
      return;
    }
    _camRemoveIpsLocally(keys);
    showToast(
      `${ips.length} camera(s) removida(s).`);
    closeCamPanel();
    updateCamTabs();
    populateCamSiteFilter();
    applyInvOltFilters();
  });

  document.getElementById('btnOltClear')?.addEventListener('click', async () => {
    const site = document.getElementById('filterSiteOlt')?.value || '';
    const mode = _invOltView || 'olt';
    const viewName = { basic: 'Basico', basico: 'Basico', olt: 'Via OLT', switch: 'Via Switch' }[mode] || mode;
    const msg = site
      ? `Apagar cameras IP do site "${site}" em ${viewName}? Esta acao nao pode ser desfeita.`
      : `Apagar todas as cameras IP em ${viewName}? Esta acao nao pode ser desfeita.`;
    if (!await showConfirm({ title: 'Apagar inventario', msg, label: 'Apagar' })) return;
    const res = await api('/api/inventory/clear', {
      method: 'POST',
      body: JSON.stringify({ mode, site, permanent: true }),
    });
    const data = await res?.json().catch(() => ({}));
    if (!res?.ok || data?.ok === false) {
      showToast(data?.detail || data?.error || 'Nao foi possivel apagar o inventario.', true);
      return;
    }
    _imgbbClear();
    if (site) {
      _invCam[mode] = (_invCam[mode] || []).filter(r => ![r.local, r.site, r.site_name].some(v => String(v || '') === site));
      _camSessionSave(mode, _invCam[mode]);
    } else {
      _invCam[mode] = [];
      try { sessionStorage.removeItem(`so_cam_${mode}`); } catch {}
    }
    updateCamTabs();
    populateCamSiteFilter();
    applyInvOltFilters();
    showToast(site ? `Site "${site}" apagado.` : 'Inventario apagado.');
  });

  // Modal editar camera
  document.getElementById('closeEditCam')?.addEventListener('click', closeEditCamModal);
  document.getElementById('cancelEditCam')?.addEventListener('click', closeEditCamModal);
  document.getElementById('saveEditCam')?.addEventListener('click', saveEditCam);

  // Modal trocar IP
  document.getElementById('trocarIpNovo')?.addEventListener('input', (ev) => {
    const ip = ev.target.value.trim();
    if (_isIpv4(ip)) _fillTrocarIpNetwork(ip, false);
  });

  document.getElementById('btnConfirmarTrocarIp')?.addEventListener('click', async () => {
    const ip    = document.getElementById('trocarIpAtual').value;
    const novo  = document.getElementById('trocarIpNovo').value.trim();
    const mask  = document.getElementById('trocarIpMask').value.trim();
    const gw    = document.getElementById('trocarIpGw').value.trim();
    const user  = document.getElementById('trocarIpUser').value.trim() || 'admin';
    const pass  = document.getElementById('trocarIpPass').value;
    const erro  = document.getElementById('trocarIpErro');
    if (!novo) { erro.textContent = 'Informe o novo IP.'; erro.hidden = false; return; }
    if (!_isIpv4(novo)) { erro.textContent = 'Novo IP invalido.'; erro.hidden = false; return; }
    if (novo === ip) { erro.textContent = 'O novo IP precisa ser diferente do IP atual.'; erro.hidden = false; return; }
    if (!mask) { erro.textContent = 'Informe a mascara da rede.'; erro.hidden = false; return; }
    if (!_isValidSubnetMask(mask)) { erro.textContent = 'Mascara de rede invalida.'; erro.hidden = false; return; }
    if (!gw) { erro.textContent = 'Informe o gateway da camera.'; erro.hidden = false; return; }
    if (!_isIpv4(gw)) { erro.textContent = 'Gateway invalido.'; erro.hidden = false; return; }
    if (_isInCameras22(novo) && (mask !== '255.255.252.0' || gw !== '10.10.10.1')) {
      erro.textContent = 'Para a rede 10.10.8.0/22 use mascara 255.255.252.0 e gateway 10.10.10.1.';
      erro.hidden = false;
      return;
    }
    if (!pass) { erro.textContent = 'Informe a senha atual da camera.'; erro.hidden = false; return; }
    const payload = { ip, new_ip: novo, user, pass, mask, gateway: gw };
    const res = await api('/api/maintenance/change_ip', { method: 'POST', body: JSON.stringify(payload) });
    const data = await res?.json().catch(() => ({}));
    if (!res?.ok || !data?.ok) {
      const detail = data?.detail || data?.error || data?.msg || data?.message || 'Erro ao trocar IP.';
      erro.textContent = detail;
      erro.hidden = false;
      return;
    }
    document.getElementById('modalTrocarIp').classList.add('hidden');
    showToast(`IP alterado para ${novo}. Aguarde a camera reconectar.`);
    loadInvOlt();
  });

  // Modal trocar senha
  document.getElementById('btnConfirmarTrocarSenha')?.addEventListener('click', async () => {
    const ip    = _invOltActive?.ip;
    const user  = document.getElementById('trocarSenhaUser').value.trim();
    const atual = document.getElementById('trocarSenhaAtual').value;
    const nova  = document.getElementById('trocarSenhaNova').value;
    const conf  = document.getElementById('trocarSenhaConfirm').value;
    const erro  = document.getElementById('trocarSenhaErro');
    if (!atual) { erro.textContent = 'Informe a senha atual.'; erro.hidden = false; return; }
    if (!nova) { erro.textContent = 'Informe a nova senha.'; erro.hidden = false; return; }
    if (nova !== conf) { erro.textContent = 'As senhas nao coincidem.'; erro.hidden = false; return; }
    const res = await api('/api/maintenance/batch/password', {
      method: 'POST',
      body: JSON.stringify({ ips: [ip], user, old_pass: atual, new_pass: nova }),
    });
    if (!res?.ok) {
      const e = await res?.json().catch(() => ({}));
      erro.textContent = e?.detail || 'Erro ao trocar senha.'; erro.hidden = false; return;
    }
    document.getElementById('modalTrocarSenha').classList.add('hidden');
    showToast('Senha alterada com sucesso!');
  });

  // Modal data/hora  alterna campos NTP vs manual
  document.getElementById('dataHoraModo')?.addEventListener('change', function() {
    document.getElementById('dataHoraNtpFields').style.display    = this.value === 'ntp' ? '' : 'none';
    document.getElementById('dataHoraManualFields').style.display = this.value === 'manual' ? '' : 'none';
  });
  document.getElementById('btnConfirmarDataHora')?.addEventListener('click', async () => {
    const ip   = _invOltActive?.ip;
    const modo = document.getElementById('dataHoraModo').value;
    const erro = document.getElementById('dataHoraErro');
    const user = document.getElementById('dataHoraUser').value.trim() || 'admin';
    const pass = document.getElementById('dataHoraPass').value;
    if (!pass) { erro.textContent = 'Informe a senha atual da camera.'; erro.hidden = false; return; }
    let res;
    if (modo === 'ntp') {
      const ntp = document.getElementById('dataHoraNtp').value.trim();
      res = await api('/api/maintenance/batch/ntp', { method: 'POST', body: JSON.stringify({ ips: [ip], user, pass, address: ntp }) });
    } else {
      const data = document.getElementById('dataHoraData').value;
      const hora = document.getElementById('dataHoraHora').value;
      res = await api('/api/maintenance/batch/ntp', { method: 'POST', body: JSON.stringify({ ips: [ip], user, pass, datetime: `${data}T${hora}:00` }) });
    }
    const body = await res?.json().catch(() => ({}));
    if (!res?.ok || body?.ok === false) {
      const first = (body?.results || []).find(r => !r.ok) || {};
      erro.textContent = body?.detail || body?.error || first.error || 'Erro ao aplicar.'; erro.hidden = false; return;
    }
    document.getElementById('modalDataHora').classList.add('hidden');
    showToast('Data/hora aplicada!');
  });

  // Filtros demais views
  document.getElementById('searchInvDvr')?.addEventListener('input', () => filterTable('searchInvDvr', 'invDvrTable'));
  document.getElementById('searchInvNvr')?.addEventListener('input', () => filterTable('searchInvNvr', 'invNvrTable'));
  document.getElementById('searchInvWindows')?.addEventListener('input', applyWindowsFilters);
  document.getElementById('filterWinStatus')?.addEventListener('change', applyWindowsFilters);
  document.getElementById('filterWinSite')?.addEventListener('change', applyWindowsFilters);
  document.getElementById('filterWinSector')?.addEventListener('change', applyWindowsFilters);
  document.getElementById('btnWinClearFilters')?.addEventListener('click', clearWinFilters);
  document.getElementById('chkWinAll')?.addEventListener('change', e => {
    const checked = e.target.checked;
    _winFilteredRows.forEach(row => {
      const key = winKey(row);
      if (!key) return;
      if (checked) _winSelected.add(key); else _winSelected.delete(key);
    });
    renderWinRows(_winFilteredRows);
  });
  document.getElementById('btnScanWindows')?.addEventListener('click', openWinScanModal);
  document.getElementById('closeWinScan')?.addEventListener('click', closeWinScanModal);
  document.getElementById('cancelWinScan')?.addEventListener('click', closeWinScanModal);
  document.getElementById('startWinScan')?.addEventListener('click', runWinScan);
  document.getElementById('btnWinAgent')?.addEventListener('click', () => downloadWithAuth('/api/windows/agent-script', 'sightops-agente-windows.ps1'));
  document.getElementById('btnWinPdf')?.addEventListener('click', () => downloadWithAuth('/api/windows/report.pdf', 'windows-inventory.pdf'));
  document.getElementById('btnWinPhotos')?.addEventListener('click', enrichWindowsPhotos);
  document.getElementById('btnWinEdit')?.addEventListener('click', openWinPhysicalModal);
  document.getElementById('btnWinDelete')?.addEventListener('click', deleteSelectedWindows);
  document.getElementById('btnWinClearAll')?.addEventListener('click', clearWindowsInventory);
  document.getElementById('closeWinPhysical')?.addEventListener('click', closeWinPhysicalModal);
  document.getElementById('cancelWinPhysical')?.addEventListener('click', closeWinPhysicalModal);
  document.getElementById('saveWinPhysical')?.addEventListener('click', saveWinPhysical);
  document.getElementById('winPanelBackdrop')?.addEventListener('click', closeWinPanel);
  document.getElementById('btnCloseWinPanel')?.addEventListener('click', closeWinPanel);
  document.getElementById('wpBtnPing')?.addEventListener('click', () => winPanelAction('ping'));
  document.getElementById('wpBtnAnydesk')?.addEventListener('click', () => winPanelAction('anydesk'));
  document.getElementById('wpBtnEdit')?.addEventListener('click', () => winPanelAction('edit'));
  document.getElementById('wpBtnAgent')?.addEventListener('click', () => winPanelAction('agent'));
  document.getElementById('wpBtnPrepare')?.addEventListener('click', () => winPanelAction('prepare'));
  document.getElementById('wpBtnPdf')?.addEventListener('click', () => winPanelAction('pdf'));
  document.getElementById('wpBtnRefresh')?.addEventListener('click', () => winPanelAction('refresh'));

  // Carrossel
  document.getElementById('carClose')?.addEventListener('click', closeCarrossel);
  document.getElementById('modalCarrossel')?.addEventListener('click', e => {
    if (e.target === document.getElementById('modalCarrossel')) closeCarrossel();
  });
  document.getElementById('carPrev')?.addEventListener('click', () => carGoTo(_carIdx - 1));
  document.getElementById('carNext')?.addEventListener('click', () => carGoTo(_carIdx + 1));
  document.getElementById('carBtnDetalhes')?.addEventListener('click', () => {
    const cam = _carCams[_carIdx];
    if (!cam) return;
    const statusColor = (cam.status||'').toLowerCase()==='online' ? 'var(--primary)' : 'var(--danger)';
    document.getElementById('camDetEyebrow').textContent = cam.ip;
    document.getElementById('camDetTitulo').textContent  = cam.titulo || '';
    document.getElementById('camDetStatus').innerHTML    = `<span style="color:${statusColor};font-weight:700">${esc(cam.status||'')}</span>`;
    document.getElementById('camDetIp').textContent      = cam.ip;
    document.getElementById('camDetLocal').textContent   = cam.local || '';
    document.getElementById('camDetMac').textContent     = cam.mac   || '';
    document.getElementById('camDetFab').textContent     = cam.fabricante || '';
    document.getElementById('camDetModelo').textContent  = cam.model  || '';
    document.getElementById('camDetPon').textContent     = [cam.pon, cam.onu_id].filter(Boolean).join(' / ') || '';
    document.getElementById('camDetOnuName').textContent = cam.onu_name   || '';
    document.getElementById('camDetOnuSer').textContent  = cam.onu_serial || '';
    const foto = document.getElementById('camDetFoto');
    foto.src = cam.snapshot_url ? `${API_BASE}${cam.snapshot_url}` : '';
    foto.style.display = cam.snapshot_url ? 'block' : 'none';
    document.getElementById('modalCamDetalhes').classList.remove('hidden');
    lucide.createIcons();
  });
  document.getElementById('closeCamDetalhes')?.addEventListener('click',  () => document.getElementById('modalCamDetalhes').classList.add('hidden'));
  document.getElementById('closeCamDetalhes2')?.addEventListener('click', () => document.getElementById('modalCamDetalhes').classList.add('hidden'));
  document.getElementById('carBtnDownload')?.addEventListener('click', () => {
    const cam = _carCams[_carIdx];
    if (!cam?.snapshot_url) { showToast('Sem foto para baixar', true); return; }
    const a = document.createElement('a');
    a.href = `${API_BASE}${cam.snapshot_url}`;
    a.download = `${(cam.titulo || cam.ip).replace(/[^a-z0-9]/gi,'_')}.jpg`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  });
  // Teclado
  document.addEventListener('keydown', e => {
    if (document.getElementById('modalCarrossel')?.style.display !== 'flex') return;
    if (e.key === 'ArrowLeft')  carGoTo(_carIdx - 1);
    if (e.key === 'ArrowRight') carGoTo(_carIdx + 1);
    if (e.key === 'Escape')     closeCarrossel();
  });

  // Snapshots Gravadores (DVR+NVR)
  document.getElementById('searchSnapGrav')?.addEventListener('input', applySnapGravFilters);
  document.getElementById('filterSnapGravTipo')?.addEventListener('change', applySnapGravFilters);
  document.getElementById('filterSnapGravStatus')?.addEventListener('change', applySnapGravFilters);
  document.getElementById('filterSnapGravSite')?.addEventListener('change', applySnapGravFilters);
  document.getElementById('btnSnapGravClearFilter')?.addEventListener('click', () => {
    document.getElementById('searchSnapGrav').value = '';
    document.getElementById('filterSnapGravTipo').value = '';
    document.getElementById('filterSnapGravStatus').value = '';
    document.getElementById('filterSnapGravSite').value = '';
    applySnapGravFilters();
  });
  document.getElementById('btnSnapGravAll')?.addEventListener('click', async () => {
    showToast('Use Varredura do gravador para atualizar snapshots com usuario e senha.', true);
  });
  document.getElementById('btnSnapGravSelected')?.addEventListener('click', async () => {
    const idxs = [...document.querySelectorAll('.chk-snap-grav:checked')].map(c => parseInt(c.value));
    if (!idxs.length) { showToast('Selecione canais para capturar', true); return; }
    showToast('Atualizacao de snapshot do gravador exige usuario e senha. Abra a varredura do gravador.', true);
  });

  // Snapshots Cameras IP
  document.getElementById('searchSnapCam')?.addEventListener('input', applySnapCamFilters);
  document.getElementById('filterSnapCamStatus')?.addEventListener('change', applySnapCamFilters);
  document.getElementById('filterSnapCamSite')?.addEventListener('change', applySnapCamFilters);
  document.getElementById('btnSnapCamClearFilter')?.addEventListener('click', () => {
    document.getElementById('searchSnapCam').value = '';
    document.getElementById('filterSnapCamStatus').value = '';
    document.getElementById('filterSnapCamSite').value = '';
    applySnapCamFilters();
  });
  document.getElementById('btnSnapCamAll')?.addEventListener('click', async () => {
    showToast('Atualizando todos os snapshots');
    await api('/api/snapshot/save', { method: 'POST', body: '{}' });
    setTimeout(loadSnapCam, 3000);
  });
  document.getElementById('btnSnapCamSelected')?.addEventListener('click', async () => {
    const ips = [...document.querySelectorAll('.chk-snap-cam:checked')].map(c => c.value);
    if (!ips.length) { showToast('Selecione cameras para capturar', true); return; }
    showToast(`Capturando ${ips.length} snapshot(s)`);
    for (const ip of ips) {
      await api('/api/snapshot/save', { method: 'POST', body: JSON.stringify({ ip }) });
    }
    setTimeout(loadSnapCam, 2000);
  });

  // Varredura DVR
  document.getElementById('btnScanDvr')?.addEventListener('click', () => {
    setRecType('dvr');
    const scanType = document.getElementById('nvrScanType');
    if (scanType) scanType.value = 'dvr';
    const scanMode = document.getElementById('nvrScanMode');
    if (scanMode) scanMode.value = _invNvrView || 'basico';
    updateNvrScanTypeLabels();
    document.getElementById('nvrScanErro').hidden = true;
    document.getElementById('modalNvrScan').classList.remove('hidden');
    refreshNvrScanConnectors().finally(updateNvrScanOriginUi);
    lucide.createIcons();
  });

  // Gravadores  seletor de tipo (NVR / DVR)
  document.querySelectorAll('[data-rec-type]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const type = btn.dataset.recType;
      setRecType(type);
      const store = _currentRecStore();
      if (!store[_invNvrView]?.length) {
        await _loadRecForMode(type, _invNvrView || 'olt');
        updateNvrTabs();
        populateNvrFilters();
        applyNvrFilters();
      }
    });
  });

  // Gravadores  tabs de visao
  document.querySelectorAll('[data-nvr-view]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const view = btn.dataset.nvrView;
      const store = _currentRecStore();
      if (!store[view]?.length) {
        await _loadRecForMode(_recType, view);
        updateNvrTabs();
      }
      setNvrView(view);
      populateNvrFilters();
      applyNvrFilters();
    });
  });

  // NVR  modal de scan dedicado
  document.getElementById('btnScanNvr')?.addEventListener('click', () => {
    const scanType = document.getElementById('nvrScanType');
    if (scanType) scanType.value = _recType || 'nvr';
    const scanMode = document.getElementById('nvrScanMode');
    if (scanMode) scanMode.value = _invNvrView || 'basico';
    updateNvrScanTypeLabels();
    document.getElementById('nvrScanErro').hidden = true;
    document.getElementById('modalNvrScan').classList.remove('hidden');
    refreshNvrScanConnectors().finally(updateNvrScanOriginUi);
    lucide.createIcons();
  });
  function updateNvrScanTypeLabels() {
    const type = _nvrScanType();
    const isDvr = type === 'dvr';
    setText('nvrScanEyebrow', isDvr ? 'Varredura DVR' : 'Varredura NVR');
    setText('nvrScanIpLabel', isDvr ? 'IP do DVR' : 'IP do NVR');
  }
  function _closeNvrModal() {
    const scanningNow = _nvrAbortCtrl !== null;
    // Para scan em andamento E remove dados parciais SO se ainda estava rodando
    if (scanningNow) {
      _nvrAbortCtrl.abort();
      _nvrAbortCtrl = null;
      _nvrUiScanning(false);
      discardActiveRecorderScan();
      _nvrActiveScan = null;
    }
    document.getElementById('nvrScanLog').innerHTML = 'Aguardando inicio';
    document.getElementById('nvrScanFooter').textContent = '';
    document.getElementById('modalNvrScan').classList.add('hidden');
  }

  document.getElementById('closeNvrScanModal')?.addEventListener('click', _closeNvrModal);
  document.getElementById('cancelNvrScan')?.addEventListener('click', _closeNvrModal);
  document.getElementById('btnStopNvrScan')?.addEventListener('click', () => {
    if (_nvrAbortCtrl) { _nvrAbortCtrl.abort(); _nvrAbortCtrl = null; }
    discardActiveRecorderScan();
    _nvrActiveScan = null;
    appendLog(document.getElementById('nvrScanLog'), '[PARADO] Cancelado pelo usuario.', 'err');
    _nvrUiScanning(false);
    setText('nvrScanFooter', '');
  });

  function _nvrPayload(extra = {}) {
    const site = (document.getElementById('nvrScanLocal')?.value || document.getElementById('filterNvrLocal')?.value || '').trim();
    const originEl = document.getElementById('nvrScanOrigin');
    const selectedConnector = document.getElementById('nvrScanConnector')?.value || '';
    const context = originEl?.value === 'connector'
      ? _networkContextForSite(site, selectedConnector)
      : { connectorId: '', hasTunnel: false };
    return {
      ip:            document.getElementById('nvrScanIp').value.trim(),
      user:          document.getElementById('nvrScanUser').value.trim() || 'admin',
      password:      document.getElementById('nvrScanPass').value,
      http_port:     parseInt(document.getElementById('nvrScanPort').value) || 80,
      start_channel: parseInt(document.getElementById('nvrScanStart').value) || 1,
      end_channel:   parseInt(document.getElementById('nvrScanEnd').value) || 32,
      timeout_sec:   4,
      inventory_mode: _nvrScanMode(),
      scan_origin: context.connectorId ? 'connector' : 'local',
      connector_id: context.connectorId || '',
      remote_connector_id: context.connectorId || '',
      remote_only: !!(context.connectorId && !context.hasTunnel),
      ...extra,
    };
  }

  async function refreshNvrScanConnectors() {
    const sel = document.getElementById('nvrScanConnector');
    if (!sel) return;
    try {
      const data = await apiJson('/api/connectors', { forceRefresh: true });
      _connectors = Array.isArray(data?.connectors) ? data.connectors : [];
    } catch {
      _connectors = _connectors || [];
    }
    const rows = _routerConnectors();
    sel.innerHTML = '<option value="">Opcional: usar servidor local/VPN</option>' + rows.map(c => {
      const online = _connectorIsOnline(c);
      const tunnel = _connectorHasTunnel(c) ? ' + VPN' : '';
      return `<option value="${esc(c.id || '')}" ${online ? '' : 'disabled'}>${esc(_connectorLabel(c))}${tunnel}${online ? '' : ' (offline)'}</option>`;
    }).join('');
    const site = (document.getElementById('nvrScanLocal')?.value || document.getElementById('filterNvrLocal')?.value || '').trim();
    const match = _findConnectorForSite(site);
    if (match?.id) {
      sel.value = match.id;
      const origin = document.getElementById('nvrScanOrigin');
      if (origin) origin.value = 'connector';
    }
  }

  function updateNvrScanOriginUi() {
    const site = (document.getElementById('nvrScanLocal')?.value || document.getElementById('filterNvrLocal')?.value || '').trim();
    const origin = document.getElementById('nvrScanOrigin');
    const connectorSel = document.getElementById('nvrScanConnector');
    const wantsConnector = origin?.value === 'connector';
    const context = wantsConnector ? _networkContextForSite(site, connectorSel?.value || '') : { connectorId: '', connector: null, online: false, hasTunnel: false };
    if (connectorSel) connectorSel.disabled = !wantsConnector;
    const status = document.getElementById('nvrScanConnectorStatus');
    if (status) {
      status.innerHTML = context.connectorId
        ? `${context.online ? '<b style="color:var(--primary)">Conector online</b>' : '<b style="color:var(--danger)">Conector offline</b>'} -- ${esc(_connectorLabel(context.connector))}${context.hasTunnel ? ' -- VPN ativa: varredura completa do gravador.' : ' -- sem VPN/rota: gravador exige acesso HTTP direto.'}`
        : 'Sem conector selecionado: usando servidor local/VPN.';
    }
  }

  function _nvrUiScanning(on) {
    const btn     = document.getElementById('btnStartNvrScan');
    const btnStop = document.getElementById('btnStopNvrScan');
    const btnDisc = document.getElementById('nvrTaskDiscoverRun');
    if (on) {
      if (btn)     { btn.disabled = true; btn.innerHTML = '<i data-lucide="loader-circle"></i> Varrendo'; }
      if (btnStop) btnStop.style.display = '';
      if (btnDisc) btnDisc.disabled = true;
    } else {
      if (btn)     { btn.disabled = false; btn.innerHTML = '<i data-lucide="scan-search"></i> Executar marcados'; }
      if (btnStop) btnStop.style.display = 'none';
      if (btnDisc) btnDisc.disabled = false;
    }
    lucide.createIcons();
  }

  async function _runNvrTask(payload) {
    const log  = document.getElementById('nvrScanLog');
    const erro = document.getElementById('nvrScanErro');
    erro.hidden = true;
    if (!payload.ip) { erro.textContent = 'Informe o IP do NVR.'; erro.hidden = false; return; }

    const local = document.getElementById('nvrScanLocal').value.trim();
    if (local) { payload.set_local = true; payload.local = local; }
    else if (payload.scan_origin === 'connector') {
      const filteredLocal = document.getElementById('filterNvrLocal')?.value || '';
      if (filteredLocal) { payload.set_local = true; payload.local = filteredLocal; }
    }

    log.innerHTML = '';
    const start = payload.start_channel || 1;
    const end   = payload.end_channel   || 32;
    appendLog(log, `[INFO] Conectando em ${payload.ip}:${payload.http_port} (canais ${start}${end})`, 'info');

    // Animacao discreta: 3 passos fixos + contador no rodape
    const steps = [
      [500,  'info', `[INFO] Autenticando como "${payload.user}"`],
      [1200, 'info', `[INFO] Lendo ${end - start + 1} canais`],
    ];
    const timers = steps.map(([d, cls, msg]) => setTimeout(() => appendLog(log, msg, cls), d));

    let secs = 0;
    const tick = setInterval(() => {
      secs++;
      setText('nvrScanFooter', `${secs}s decorridos`);
    }, 1000);

    _nvrAbortCtrl = new AbortController();
    _nvrActiveScan = {
      type: _nvrScanType(),
      host: payload.ip,
      start,
      end,
    };
    _nvrUiScanning(true);

    let res = null;
    try {
      const scanType = _nvrScanType();
      const endpoint = scanType === 'dvr' ? '/api/dvr/scan' : '/api/nvr/scan';
      res = await api(endpoint, {
        method: 'POST',
        body: JSON.stringify(payload),
        skipLogout: true,
        signal: _nvrAbortCtrl.signal,
      });
    } catch (e) {
      if (e.name === 'AbortError') {
        appendLog(log, '[PARADO] Varredura cancelada.', 'err');
        timers.forEach(t => clearTimeout(t));
        clearInterval(tick);
        _nvrUiScanning(false);
        setText('nvrScanFooter', '');
        _nvrActiveScan = null;
        return;
      }
      throw e;
    }

    timers.forEach(t => clearTimeout(t));
    clearInterval(tick);
    _nvrAbortCtrl = null; // scan terminou  fechar nao apaga mais dados
    _nvrActiveScan = null;
    _nvrUiScanning(false);
    setText('nvrScanFooter', '');

    if (res?.ok) {
      const data = await res.json();
      const mode  = _nvrScanMode();
      const stype = _nvrScanType();
      let rows = data?.inventory || [];
      appendLog(log, `[OK] Scan concluido  ${rows.length} canais encontrados.`, 'ok');
      clearApiJsonCache('/api/nvr/inventory');
      clearApiJsonCache('/api/dvr/inventory');
      try {
        await _loadRecAllModesForType(stype);
      } catch {}

      // Enriquece se necessario
      if ((mode === 'olt' || mode === 'switch') && rows.length) {
        appendLog(log, '[INFO] Cruzando com inventario de cameras', 'info');
        rows = await enrichRecRowsForMode(rows, mode);
        appendLog(log, `[OK] Cruzamento concluido.`, 'ok');
      }

      // Salva no store correto (NVR ou DVR) e sincroniza o tipo na UI
      const store = stype === 'dvr' ? _invDvr : _invNvr;
      const payloadConnector = String(payload.remote_connector_id || payload.connector_id || '');
      store[mode] = [...store[mode].filter(r => {
        const sameHost = String(r.host || '') === String(payload.ip || '');
        const rowConnector = String(r.remote_connector_id || r.connector_id || '');
        if (!sameHost) return true;
        if (payloadConnector || rowConnector) return rowConnector !== payloadConnector;
        return false;
      }), ...rows];
      _recSessionSave(stype, mode, store[mode]);
      pruneSyntheticRecModes(stype);
      setRecType(stype);
      if ((store[mode] || []).length) setNvrView(mode);
      populateNvrFilters();
      applyNvrFilters();
    } else {
      const e = await res?.json().catch(() => ({}));
      const msg = e?.detail || (res?.status === 401 ? 'Credenciais invalidas para o NVR.' : 'Erro na varredura.');
      appendLog(log, '[ERRO] ' + msg, 'err');
    }
  }

  function _nvrScanMode() { return document.getElementById('nvrScanMode')?.value || 'basico'; }
  function _nvrScanType() { return document.getElementById('nvrScanType')?.value || 'nvr'; }
  document.getElementById('nvrScanType')?.addEventListener('change', updateNvrScanTypeLabels);
  document.getElementById('nvrScanOrigin')?.addEventListener('change', updateNvrScanOriginUi);
  document.getElementById('nvrScanConnector')?.addEventListener('change', updateNvrScanOriginUi);
  document.getElementById('nvrScanLocal')?.addEventListener('input', () => refreshNvrScanConnectors().finally(updateNvrScanOriginUi));

  async function _runNvrScan(extra = {}) {
    document.getElementById('nvrScanLog').innerHTML = '';
    await _runNvrTask(_nvrPayload(extra));
    // Dados ja atualizados dentro de _runNvrTask via data.inventory
  }

  document.getElementById('btnStartNvrScan')?.addEventListener('click', () =>
    _runNvrScan({ imgbb: document.getElementById('nvrTaskImgbb').checked }));

  document.getElementById('nvrTaskDiscoverRun')?.addEventListener('click', () =>
    _runNvrScan({ imgbb: false }));
  document.getElementById('nvrTaskSnapshotRun')?.addEventListener('click', async () => {
    const ip = document.getElementById('nvrScanIp').value.trim();
    const stype = _nvrScanType();
    if (!ip) { showToast(`Informe o IP do ${stype === 'dvr' ? 'DVR' : 'NVR'}`, true); return; }
    appendLog(document.getElementById('nvrScanLog'), 'Capturando snapshots', 'info');
    await api(`/api/${stype}/snapshot/update`, { method: 'POST', body: JSON.stringify({ ip, user: document.getElementById('nvrScanUser').value, password: document.getElementById('nvrScanPass').value }) });
    appendLog(document.getElementById('nvrScanLog'), ' Snapshots atualizados.', 'ok');
    loadInvNvr();
  });
  document.getElementById('nvrTaskImgbbRun')?.addEventListener('click', async () => {
    const stype = _nvrScanType();
    appendLog(document.getElementById('nvrScanLog'), 'Enviando ao ImgBB', 'info');
    const res = await api(`/api/${stype}/imgbb/upload`, { method: 'POST', body: '{}' });
    const d = await res?.json().catch(() => ({}));
    appendLog(document.getElementById('nvrScanLog'), ` ${d?.uploaded ?? '?'} fotos enviadas.`, 'ok');
    loadInvNvr();
  });
  document.getElementById('btnNvrClear')?.addEventListener('click', async () => {
    const store      = _currentRecStore();
    const typeName   = _recType === 'dvr' ? 'Analogico (DVR)' : 'NVR  IP';
    const viewName   = { basico: 'Basico', olt: 'Via OLT', switch: 'Via Switch' }[_invNvrView] || _invNvrView;
    const siteFilter = document.getElementById('filterNvrLocal')?.value || '';
    const hostFilter = document.getElementById('filterNvrHost')?.value  || '';
    const hasFilter  = !!(siteFilter || hostFilter);

    const scopeMsg = hasFilter
      ? `Apagar canais de ${typeName}  ${viewName}${siteFilter ? `  site "${siteFilter}"` : ''}${hostFilter ? `  host "${hostFilter}"` : ''}?`
      : `Apagar TODOS os canais de ${typeName}  ${viewName}?`;

    if (!await showConfirm({ title: 'Apagar dados', msg: scopeMsg, label: 'Apagar' })) return;

    const endpoint = _recType === 'dvr' ? '/api/dvr/clear' : '/api/nvr/clear';
    const params = new URLSearchParams();
    params.set('mode', _invNvrView || 'basico');
    if (siteFilter) params.set('site', siteFilter);
    if (hostFilter) params.set('host', hostFilter);
    const res = await api(`${endpoint}?${params.toString()}`, { method: 'POST', body: '{}' });
    const data = await res?.json().catch(() => ({}));
    if (!res?.ok || data?.ok === false) {
      showToast(data?.detail || data?.error || 'Nao foi possivel apagar os dados.', true);
      return;
    }
    store[_invNvrView] = (store[_invNvrView] || []).filter(r => {
      if (siteFilter && ![r.local, r.site, r.site_name].some(v => String(v || '') === siteFilter)) return true;
      if (hostFilter && String(r.host || '') !== hostFilter) return true;
      if (!siteFilter && !hostFilter) return false;
      return false;
    });
    _recSessionSave(_recType, _invNvrView, store[_invNvrView]);

    updateNvrTabs();
    populateNvrFilters();
    applyNvrFilters();
    showToast('Dados apagados.');
  });
  document.getElementById('searchInvNvr')?.addEventListener('input', applyNvrFilters);
  document.getElementById('filterNvrStatus')?.addEventListener('change', applyNvrFilters);
  document.getElementById('filterNvrLocal')?.addEventListener('change', applyNvrFilters);
  document.getElementById('filterNvrHost')?.addEventListener('change', applyNvrFilters);
  document.getElementById('btnNvrClearFilter')?.addEventListener('click', () => {
    document.getElementById('searchInvNvr').value = '';
    document.getElementById('filterNvrStatus').value = '';
    document.getElementById('filterNvrLocal').value  = '';
    document.getElementById('filterNvrHost').value   = '';
    applyNvrFilters();
  });
  document.getElementById('btnNvrPdf')?.addEventListener('click', (event) => runNvrReport(event.currentTarget));
  document.getElementById('btnNvrImgbb')?.addEventListener('click', async () => {
    const selected = selectedRecItems();
    if (!selected.length) { showToast('Selecione ao menos um canal', true); return; }
    const endpoint = _recType === 'dvr' ? '/api/dvr/imgbb/upload' : '/api/nvr/imgbb/upload';
    showToast(`Enviando ${selected.length} canal(is) ao ImgBB...`);
    const res = await api(endpoint, { method: 'POST', body: JSON.stringify({ selected }) });
    const d = await res?.json().catch(() => ({}));
    if (!res?.ok || d?.ok === false) {
      showToast(d?.detail || d?.error || 'Erro ao enviar ao ImgBB.', true);
      return;
    }
    showToast(`Concluido: ${d?.uploaded ?? 0} foto(s) enviada(s) ao ImgBB.`);
    if (Array.isArray(d?.inventory)) applyRecPayloadsLocally(d.inventory, _recType);
    updateNvrTabs();
    populateNvrFilters();
    applyNvrFilters();
  });
  document.getElementById('btnNvrEditar')?.addEventListener('click', () => {
    const rows = selectedRecRows();
    if (!rows.length) { showToast('Selecione ao menos um canal para editar', true); return; }
    openEditRecModal(rows);
  });
  document.getElementById('closeEditRec')?.addEventListener('click', closeEditRecModal);
  document.getElementById('cancelEditRec')?.addEventListener('click', closeEditRecModal);
  document.getElementById('saveEditRec')?.addEventListener('click', saveEditRec);
  document.getElementById('btnNvrDeleteSelected')?.addEventListener('click', async () => {
    const items = selectedRecItems();
    if (!items.length) { showToast('Selecione ao menos um canal', true); return; }
    if (!await showConfirm({ title: 'Apagar canais', msg: `Remover ${items.length} canal(is) do inventario?`, label: 'Remover' })) return;
    const endpoint = _recType === 'dvr' ? '/api/dvr/delete' : '/api/nvr/delete';
    const res = await api(endpoint, { method: 'POST', body: JSON.stringify({ items, mode: _invNvrView || 'basico' }) });
    const d = await res?.json().catch(() => ({}));
    if (!res?.ok || d?.ok === false) {
      showToast(d?.detail || d?.error || 'Erro ao remover canais.', true);
      return;
    }
    showToast(`${d?.removed ?? items.length} canal(is) removido(s).`);
    removeRecItemsLocally(_recType, items, _invNvrView || 'basico');
    closeRecPanel();
    updateNvrTabs();
    populateNvrFilters();
    applyNvrFilters();
  });
  document.getElementById('btnCloseRecPanel')?.addEventListener('click', closeRecPanel);
  document.getElementById('recPanelBackdrop')?.addEventListener('click', closeRecPanel);
  document.getElementById('rpBtnAtualizar')?.addEventListener('click', () => recPanelAction('snapshot'));
  document.getElementById('rpBtnRenomear')?.addEventListener('click', () => recPanelAction('rename'));
  document.getElementById('rpBtnTrocarIp')?.addEventListener('click', () => recPanelAction('ip'));
  document.getElementById('rpBtnTrocarSenha')?.addEventListener('click', () => recPanelAction('password'));
  document.getElementById('rpBtnDataHora')?.addEventListener('click', () => recPanelAction('datetime'));
  document.getElementById('rpBtnReboot')?.addEventListener('click', () => recPanelAction('reboot'));
  document.getElementById('rpBtnWeb')?.addEventListener('click', () => recPanelAction('web'));
  document.getElementById('rpBtnPing')?.addEventListener('click', () => recPanelAction('ping'));
  document.getElementById('closeRecAction')?.addEventListener('click', closeRecAction);
  document.getElementById('cancelRecAction')?.addEventListener('click', closeRecAction);
  document.getElementById('confirmRecAction')?.addEventListener('click', runRecAction);
  document.getElementById('recActionPass')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') runRecAction();
  });

  // O mapa que sera aplicado e ESCOLHIDO aqui. Antes o Aplicar usava sempre o
  // ultimo KMZ importado, sem dizer -- e foi assim que o mapa de um site acabou
  // aplicado achando que era de outro.
  async function carregarMapasKMZ() {
    const sel = document.getElementById('mapApplyLayer');
    if (!sel) return;
    const escolhido = sel.value;
    const res = await api('/api/kmz/import/layers?include_features=false');
    const data = await res?.json().catch(() => ({}));
    const mapas = data?.layers || [];
    if (!mapas.length) {
      sel.innerHTML = '<option value="">nenhum mapa importado</option>';
      document.getElementById('mapApplyLayerInfo').textContent = 'Importe um KMZ no passo 1 para comecar.';
      return;
    }
    sel.innerHTML = mapas
      .map(m => `<option value="${m.id}">${m.label} (${m.features_count} pontos)</option>`)
      .join('');
    // Mantem a escolha do usuario entre recargas; senao, o mais recente.
    sel.value = mapas.some(m => m.id === escolhido) ? escolhido : mapas[0].id;
  }

  async function carregarResumoMapa() {
    const info    = document.getElementById('mapApplyLayerInfo');
    const elLabel = document.getElementById('mapApplyOverwriteLabel');
    const detalhe = document.getElementById('mapApplySemPonto');
    const layerId = document.getElementById('mapApplyLayer')?.value || '';
    if (!info) return;

    info.textContent = 'conferindo...';
    if (elLabel) elLabel.textContent = 'Sobrescrever coordenadas existentes';
    detalhe?.classList.add('hidden');

    const source = document.getElementById('mapApplySource')?.value || 'ip';
    const mode   = typeof mapInventoryMode === 'function' ? mapInventoryMode() : 'olt';
    const res  = await api('/api/kmz/import/locations/apply', { method: 'POST', body: JSON.stringify({ source, mode, layer_id: layerId, overwrite: false, dry_run: true }) });
    const data = await res?.json().catch(() => ({}));

    if (!data || data.error || data.detail) {
      info.textContent = data?.detail || data?.error || 'nao consegui ler esse mapa.';
      return;
    }

    const site = (data.site || '').toUpperCase();
    if (site) {
      const casam = (data.updated || 0) + (data.skipped_has_loc || 0);
      // Duas linhas: o que ESTE mapa faz, e o que ele nao encosta. Numa linha so
      // virava um paragrafo e o dado importante se perdia no meio.
      info.innerHTML = `Site: <strong>${site}</strong> &middot; casam <strong>${casam}</strong> de ${data.points_total} pontos`
        + (data.fora_do_site ? `<div style="opacity:.75">${data.fora_do_site} camera(s) de outros sites nao serao tocadas</div>` : '');
    } else {
      // Sem site reconhecido o backend recusa os criterios fracos de match.
      info.innerHTML = `<span style="color:var(--warning,#d98600)">Nao reconheci o site deste mapa. So casa por nome exato ou IP.</span>`;
    }

    if (elLabel) {
      const jaTem = data.skipped_has_loc || 0;
      elLabel.textContent = jaTem ? `Sobrescrever as ${jaTem} que ja tem coordenada` : 'Sobrescrever coordenadas existentes';
    }
    renderSemPonto(data.no_match_rows || []);
  }

  function renderSemPonto(linhas) {
    const bloco = document.getElementById('mapApplySemPonto');
    const lista = document.getElementById('mapApplySemPontoLista');
    if (!bloco || !lista) return;
    if (!linhas.length) { bloco.classList.add('hidden'); return; }
    bloco.classList.remove('hidden');
    bloco.querySelector('summary').textContent = `${linhas.length} camera(s) deste site sem ponto no mapa`;
    lista.innerHTML = linhas
      .map(r => `<div>${(r.titulo || r.local || '(sem titulo)')} <span style="opacity:.7">- ${r.ip || 's/ IP'}</span></div>`)
      .join('');
  }

  // Botao Ferramentas KMZ
  document.getElementById('btnMapTools')?.addEventListener('click', () => {
    const modeSel = document.getElementById('mapInventoryMode');
    if (modeSel && !modeSel.value) modeSel.value = _invOltView || 'olt';
    document.getElementById('modalMapTools').classList.remove('hidden');
    lucide.createIcons();
    carregarMapasKMZ().then(carregarResumoMapa);
  });
  document.getElementById('mapApplySource')?.addEventListener('change', carregarResumoMapa);
  document.getElementById('mapApplyLayer')?.addEventListener('change', carregarResumoMapa);
  document.getElementById('closeMapTools')?.addEventListener('click',  () => document.getElementById('modalMapTools').classList.add('hidden'));
  document.getElementById('closeMapTools2')?.addEventListener('click', () => document.getElementById('modalMapTools').classList.add('hidden'));

  // Etapa 2  Previa e Aplicar coordenadas
  document.getElementById('btnMapApplyPreview')?.addEventListener('click', async () => {
    const source   = document.getElementById('mapApplySource')?.value || 'ip';
    const mode     = typeof mapInventoryMode === 'function' ? mapInventoryMode() : 'olt';
    const overwrite = document.getElementById('mapApplyOverwrite')?.checked || false;
    const status   = document.getElementById('mapApplyStatus');
    status.textContent = 'Calculando previa';
    const layerId = document.getElementById('mapApplyLayer')?.value || '';
    const res  = await api('/api/kmz/import/locations/apply', { method: 'POST', body: JSON.stringify({ source, mode, layer_id: layerId, overwrite, dry_run: true }) });
    const data = await res?.json().catch(() => ({}));
    if (data?.error) { status.textContent = ' ' + data.error; status.style.color = 'var(--danger)'; return; }
    const src = document.getElementById('mapApplySource')?.options[document.getElementById('mapApplySource')?.selectedIndex]?.text || source;
    status.style.color = 'var(--muted)';
    const alvo = (data.site || '').toUpperCase();
    status.innerHTML = `<strong>${src}</strong>${alvo ? ` em <strong>${alvo}</strong>` : ''} | Pontos: ${data.points_total ?? '?'}`
      + ` | Ganhariam coordenada: ${data.updated ?? '?'} | Sem ponto no mapa: ${data.no_match ?? '?'}`
      + ` | Ja tinham: ${data.skipped_has_loc ?? '?'}`
      + (data.fora_do_site ? ` | De outros sites (intocadas): ${data.fora_do_site}` : '');
    renderSemPonto(data.no_match_rows || []);
  });

  document.getElementById('btnMapApply')?.addEventListener('click', async () => {
    const source    = document.getElementById('mapApplySource')?.value || 'ip';
    const mode      = typeof mapInventoryMode === 'function' ? mapInventoryMode() : 'olt';
    const overwrite = document.getElementById('mapApplyOverwrite')?.checked || false;
    const status    = document.getElementById('mapApplyStatus');
    status.textContent = 'Aplicando'; status.style.color = 'var(--muted)';
    const layerId = document.getElementById('mapApplyLayer')?.value || '';
    const res  = await api('/api/kmz/import/locations/apply', { method: 'POST', body: JSON.stringify({ source, mode, layer_id: layerId, overwrite }) });
    const data = await res?.json().catch(() => ({}));
    if (data?.error) { status.textContent = ' ' + data.error; status.style.color = 'var(--danger)'; return; }
    status.style.color = 'var(--primary)';
    const src = document.getElementById('mapApplySource')?.options[document.getElementById('mapApplySource')?.selectedIndex]?.text || source;
    const alvo = (data.site || '').toUpperCase();
    status.innerHTML = ` <strong>${src}</strong>${alvo ? ` em <strong>${alvo}</strong>` : ''} | Atualizadas: ${data.updated ?? '?'}`
      + ` | Sem ponto no mapa: ${data.no_match ?? '?'}`
      + (data.fora_do_site ? ` | De outros sites (intocadas): ${data.fora_do_site}` : '');
    showToast(`${data.updated ?? '?'} cameras${alvo ? ' de ' + alvo : ''} atualizadas com GPS!`);
    carregarResumoMapa();
  });

  // Etapa 3  Gerar KMZ
  document.getElementById('btnMapViewGenerated')?.addEventListener('click', async () => {
    const status = document.getElementById('mapGenerateStatus');
    status.textContent = 'Carregando camada gerada'; status.style.color = 'var(--muted)';
    await loadMapLayers();
    const lastGenerated = sessionStorage.getItem('so_kmz_last_generated_layer') || '';
    const generatedId = lastGenerated ? `generated:${lastGenerated}` : Object.keys(_mapLayerGroups).find(id => id.startsWith('generated:'));
    const generatedState = generatedId ? _mapLayerGroups[generatedId] : null;
    if (generatedState && !generatedState.active) await toggleMapLayer(generatedId, generatedState.def);
    status.textContent = generatedState ? 'Camada gerada exibida no mapa.' : 'Nenhuma camada gerada encontrada.';
    status.style.color = generatedState ? 'var(--primary)' : 'var(--danger)';
  });

  document.getElementById('btnMapDownloadGenerated')?.addEventListener('click', () => {
    const lastGenerated = sessionStorage.getItem('so_kmz_last_generated_layer') || '';
    const url = lastGenerated ? `/api/kmz/generated/layers/${encodeURIComponent(lastGenerated)}/download` : '/api/kmz/generated/download';
    downloadWithAuth(url, 'cameras-gerado.kmz');
  });

  // Mapa
  const mapModeSel = document.getElementById('mapInventoryMode');
  if (mapModeSel) {
    const savedMapMode = sessionStorage.getItem('so_kmz_inventory_mode') || _invOltView || 'olt';
    if (['basico', 'olt', 'switch'].includes(savedMapMode)) mapModeSel.value = savedMapMode;
    mapModeSel.addEventListener('change', () => {
      sessionStorage.setItem('so_kmz_inventory_mode', mapModeSel.value || 'olt');
      loadMapLayers();
    });
  }
  document.getElementById('mapFilterStatus')?.addEventListener('change', loadMapLayers);
  document.getElementById('mapFilterType')?.addEventListener('change', loadMapLayers);
  document.getElementById('mapFilterSite')?.addEventListener('change', loadMapLayers);
  document.getElementById('btnMapReload')?.addEventListener('click', async () => {
    await refreshMapLiveStatus();
    await loadKmz();
  });

  // Importar KMZ
  document.getElementById('btnMapImport')?.addEventListener('click', () =>
    document.getElementById('mapKmzInput')?.click());

  document.getElementById('mapKmzInput')?.addEventListener('change', async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;

    // Passo 1: importar o arquivo
    showToast(`Importando ${file.name}`);
    const form = new FormData();
    form.append('file', file);
    const headers = {};
    if (_token) headers['Authorization'] = `Bearer ${_token}`;

    let importRes;
    try {
      importRes = await fetch(`${API_BASE}/api/kmz/import`, { method: 'POST', credentials: 'same-origin', headers, body: form });
    } catch (err) {
      showToast('Erro de conexao ao importar KMZ', true);
      e.target.value = '';
      return;
    }

    if (!importRes.ok) {
      const text = await importRes.text().catch(() => '');
      let detail = 'Erro desconhecido';
      try { detail = JSON.parse(text)?.detail || text || detail; } catch { detail = text || detail; }
      showToast('Erro ao importar: ' + detail.slice(0, 120), true);
      console.error('KMZ import error', importRes.status, text);
      e.target.value = '';
      return;
    }

    const importData = await importRes.json().catch(() => ({}));
    const featCount  = importData?.features_count ?? importData?.total ??
      importData?.features?.length ?? importData?.count ?? '?';
    // Salva o nome do arquivo para mostrar na camada
    const kmlName = file.name.replace(/\.(kmz|kml)$/i, '');
    sessionStorage.setItem('so_kmz_imported_name', kmlName);
    if (importData?.id) sessionStorage.setItem('so_kmz_current_import_layer', importData.id);
    showToast(`KMZ importado  ${featCount} pontos encontrados`);
    // Entrou mapa novo: ele tem que aparecer na lista, ja selecionado.
    await carregarMapasKMZ();
    const selNovo = document.getElementById('mapApplyLayer');
    if (selNovo && importData?.id) selNovo.value = importData.id;
    carregarResumoMapa();

    // Passo 2: perguntar se quer aplicar ao inventario
    const apply = await showConfirm({
      eyebrow: 'KMZ importado',
      title:   'Aplicar localizacoes?',
      msg:     `O KMZ tem ${featCount} ponto(s). Deseja aplicar as coordenadas GPS ao inventario de cameras?`,
      label:   'Aplicar',
      danger:  false,
    });

    if (apply) {
      const applyRes = await api('/api/kmz/import/locations/apply', {
        method: 'POST',
        body: JSON.stringify({ source: 'ip', mode: typeof mapInventoryMode === 'function' ? mapInventoryMode() : 'olt', overwrite: true }),
      });
      if (applyRes?.ok) {
        const d = await applyRes.json().catch(() => ({}));
        showToast(`Localizacoes aplicadas  ${d?.updated ?? '?'} cameras atualizadas!`);
      } else {
        const err = await applyRes?.json().catch(() => ({}));
        showToast('Erro ao aplicar: ' + (err?.detail || 'verifique o inventario'), true);
      }
    }

    await loadKmz();
    e.target.value = '';
  });
  document.getElementById('btnMapDownloadKmz')?.addEventListener('click', () =>
    downloadWithAuth('/api/kmz/import/download', 'imported.kmz'));
  document.getElementById('btnMapGenerate')?.addEventListener('click', async () => {
    const genStatus = document.getElementById('mapGenerateStatus');
    const genName = document.getElementById('mapGenerateName')?.value.trim() || 'Cameras do Inventario';
    const mode = typeof mapInventoryMode === 'function' ? mapInventoryMode() : 'olt';
    const sourceLayerId = sessionStorage.getItem('so_kmz_current_import_layer') || '';
    if (genStatus) { genStatus.textContent = 'Gerando'; genStatus.style.color = 'var(--muted)'; }
    sessionStorage.setItem('so_kmz_generated_name', genName);
    const res = await api('/api/kmz/generate', {
      method: 'POST',
      body: JSON.stringify({ source: 'ip', mode, label: genName, layer_id: sourceLayerId }),
    });
    if (res?.ok) {
      const data = await res.json().catch(() => ({}));
      if (data?.id) sessionStorage.setItem('so_kmz_last_generated_layer', data.id);
      if (genStatus) { genStatus.textContent = ' Gerado com sucesso!'; genStatus.style.color = 'var(--primary)'; }
      showToast('KMZ gerado!');
      await loadMapLayers();
      const generatedId = data?.id ? `generated:${data.id}` : Object.keys(_mapLayerGroups).find(id => id.startsWith('generated:'));
      const generatedState = generatedId ? _mapLayerGroups[generatedId] : null;
      if (generatedState && !generatedState.active) await toggleMapLayer(generatedId, generatedState.def);
    } else {
      if (genStatus) { genStatus.textContent = ' Erro ao gerar.'; genStatus.style.color = 'var(--danger)'; }
      showToast('Erro ao gerar KMZ', true);
    }
  });

  // OLT  abre modal de configuracao
  document.getElementById('btnOltCollect')?.addEventListener('click', () => {
    openOltCollectModal();
  });
  document.getElementById('oltOrigin')?.addEventListener('change', updateOltOriginUi);
  document.getElementById('oltConnector')?.addEventListener('change', updateOltOriginUi);
  document.getElementById('oltSite')?.addEventListener('input', () => {
    refreshOltConnectors().finally(updateOltOriginUi);
  });
  document.getElementById('closeOltModal')?.addEventListener('click', () => document.getElementById('modalOltCollect').classList.add('hidden'));
  document.getElementById('cancelOltModal')?.addEventListener('click', () => document.getElementById('modalOltCollect').classList.add('hidden'));
  document.getElementById('btnOltStart')?.addEventListener('click', () => {
    document.getElementById('modalOltCollect').classList.add('hidden');
    oltCollect();
  });

  document.getElementById('btnOltClearTable')?.addEventListener('click', async () => {
    const site = document.getElementById('oltFilterSite')?.value || '';
    const ok = await showConfirm({
      eyebrow: 'Tabela OLT',
      title:   site ? `Apagar site "${site}"` : 'Apagar tudo',
      msg:     site
        ? `Serao removidos todos os registros do site "${site}". Esta acao nao pode ser desfeita.`
        : 'Serao removidos todos os registros de todos os sites. Esta acao nao pode ser desfeita.',
      label: 'Apagar',
    });
    if (!ok) return;
    await api(`/api/olt/clear${site ? `?site=${encodeURIComponent(site)}` : ''}`, { method: 'POST', body: '{}' });
    _oltRows = site ? _oltRows.filter(r => r.site !== site) : [];
    renderOltTable(_oltRows);
    populateOltMacSiteFilter();
    showToast(site ? `Site "${site}" apagado.` : 'Tabela OLT apagada.');
  });

  // Filtros OLT
  document.getElementById('oltFilterSite')?.addEventListener('change', filterOltTable);
  document.getElementById('oltSearch')?.addEventListener('input', filterOltTable);
  document.getElementById('btnOltMacsClearFilter')?.addEventListener('click', () => {
    document.getElementById('oltSearch').value = '';
    document.getElementById('oltFilterSite').value = '';
    filterOltTable();
  });

  // Terminal OLT
  document.getElementById('oltTermClear')?.addEventListener('click', () => { const el = document.getElementById('oltConsole'); if (el) el.innerHTML = ''; });
  document.getElementById('oltTermClose')?.addEventListener('click', () => document.getElementById('oltTerminal').classList.add('hidden'));

  // Coleta Switch
  document.getElementById('btnScanSwitch')?.addEventListener('click', openSwitchCollectModal);
  document.getElementById('closeSwitchModal')?.addEventListener('click', closeSwitchCollectModal);
  document.getElementById('cancelSwitchModal')?.addEventListener('click', closeSwitchCollectModal);
  document.getElementById('btnSwitchStart')?.addEventListener('click', switchCollect);
  document.getElementById('switchConnector')?.addEventListener('change', updateSwitchConnectorUi);
  document.getElementById('switchSite')?.addEventListener('input', () => {
    refreshSwitchConnectors().finally(updateSwitchConnectorUi);
  });

  // Filtros Switch
  document.getElementById('switchFilterSite')?.addEventListener('change', filterSwitchTable);
  document.getElementById('switchFilterDevice')?.addEventListener('change', filterSwitchTable);
  document.getElementById('switchFilterStatus')?.addEventListener('change', filterSwitchTable);
  document.getElementById('switchSearch')?.addEventListener('input', filterSwitchTable);
  document.getElementById('btnSwitchClearFilter')?.addEventListener('click', () => {
    document.getElementById('switchSearch').value = '';
    document.getElementById('switchFilterSite').value = '';
    document.getElementById('switchFilterDevice').value = '';
    document.getElementById('switchFilterStatus').value = '';
    filterSwitchTable();
  });
  document.getElementById('switchTable')?.addEventListener('click', (ev) => {
    const btn = ev.target.closest('.switch-port-action');
    if (btn) switchPortAction(btn);
  });
  document.getElementById('btnSwitchClearTable')?.addEventListener('click', async () => {
    const site = document.getElementById('switchFilterSite')?.value || '';
    const ok = await showConfirm({
      eyebrow: 'Tabela Switch',
      title:   site ? `Apagar site "${site}"` : 'Apagar tudo',
      msg:     site
        ? `Serao removidos todos os registros do site "${site}". Esta acao nao pode ser desfeita.`
        : 'Serao removidos todos os registros de todos os sites. Esta acao nao pode ser desfeita.',
      label: 'Apagar',
    });
    if (!ok) return;
    await api(`/api/switch/clear${site ? `?site=${encodeURIComponent(site)}` : ''}`, { method: 'POST', body: '{}' });
    _switchRows = site ? _switchRows.filter(r => r.site !== site) : [];
    populateSwitchFilters();
    renderSwitchTable(_switchRows);
    showToast(site ? `Site "${site}" apagado.` : 'Tabela Switch apagada.');
  });

  // Scripts
  document.getElementById('btnGenGrafana')?.addEventListener('click', async () => {
    const url       = document.getElementById('gfUrl')?.value.trim();
    const apiKey    = document.getElementById('gfApiKey')?.value.trim();
    const folderUid = document.getElementById('gfFolderUid')?.value.trim();
    const overwrite = document.getElementById('gfOverwrite')?.checked ?? true;

    if (!url || !apiKey) {
      showToast('Preencha a URL e a API Key do Grafana', true);
      return;
    }

    const log   = document.getElementById('grafanaLog');
    const badge = document.getElementById('gfStatusBadge');
    const btn   = document.getElementById('btnGenGrafana');

    log.textContent = 'Conectando ao Grafana\n';
    badge.style.display = 'none';
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-circle"></i> Importando';
    lucide.createIcons();

    const res  = await api('/api/scripts/grafana', {
      method: 'POST',
      body: JSON.stringify({ url, api_key: apiKey, folder_uid: folderUid, overwrite }),
    });
    const data = await res?.json().catch(() => ({}));

    btn.disabled = false;
    btn.innerHTML = '<i data-lucide="bar-chart-2"></i> Importar Dashboard';
    lucide.createIcons();

    if (data?.error) {
      log.textContent = ' Erro: ' + data.error + '\n\n' + (data.stderr || '') + (data.stdout || '');
      badge.textContent = 'Erro';
      badge.style.background = 'var(--danger-soft)';
      badge.style.color = 'var(--danger)';
      badge.style.display = 'inline-block';
    } else {
      log.textContent = data?.stdout || data?.result || 'Concluido.';
      if (data?.stderr) log.textContent += '\n\n[stderr]\n' + data.stderr;
      badge.textContent = ' Importado';
      badge.style.background = 'var(--primary-soft)';
      badge.style.color = 'var(--primary)';
      badge.style.display = 'inline-block';
      showToast('Dashboard importado no Grafana!');
    }
  });
// Templates padrao por fonte
  const ZBX_TEMPLATES = {
    'ip':         'Template Module ICMP Ping',
    'ip-olt':     'Template Module ICMP Ping',
    'ip-switch':  'Template Module ICMP Ping',
    'dvr':        'Template Cam-Snapshot DVR Channel',
    'nvr':        'Template Cam-Snapshot DVR Channel',
    'nvr-olt':    'Template Cam-Snapshot DVR Channel',
    'nvr-switch': 'Template Cam-Snapshot DVR Channel',
  };

  // Zabbix  mostra/oculta campos DVR/Telegram e atualiza template
  document.getElementById('zbxSource')?.addEventListener('change', function() {
    const v = this.value;
    const isDvr = v === 'dvr' || v.startsWith('nvr');
    document.getElementById('zbxDvrPanel').style.display = isDvr ? 'block' : 'none';
    const tmpl = document.getElementById('zbxTemplate');
    if (tmpl) tmpl.value = ZBX_TEMPLATES[v] || 'Template Module ICMP Ping';
  });
  document.getElementById('zbxTgAuto')?.addEventListener('change', function() {
    document.getElementById('zbxTgFields').style.display = this.checked ? 'block' : 'none';
  });

  document.getElementById('btnGenZabbix')?.addEventListener('click', async () => {
    const url      = document.getElementById('zbxUrl')?.value.trim();
    const user     = document.getElementById('zbxUser')?.value.trim();
    const pass     = document.getElementById('zbxPass')?.value;
    const sourceUI = document.getElementById('zbxSource')?.value || 'ip';
    // Mapeia para source e inv_mode separados
    const source  = sourceUI.startsWith('ip')  ? 'ip'
                  : sourceUI.startsWith('nvr') ? 'nvr'
                  : sourceUI; // dvr
    const invMode = sourceUI.endsWith('-olt')    ? 'olt'
                  : sourceUI.endsWith('-switch') ? 'switch'
                  : 'basic';
    const group    = document.getElementById('zbxGroup')?.value.trim() || 'Cameras';
    const template = document.getElementById('zbxTemplate')?.value.trim() || 'Template Module ICMP Ping';
    // o campo aceita varios: "" (ou nada marcado) = todos os sites
    const seletorSite = document.getElementById('zbxSite');
    const sites = seletorSite
      ? [...seletorSite.selectedOptions].map(o => o.value).filter(Boolean)
      : [];
    const site = sites.length === 1 ? sites[0] : '';
    // sem nenhum site marcado, a lista vazia seria lida como "todos" e
    // sincronizaria o inventario inteiro sem o usuario ter pedido
    if (!sites.length && window._zbxTodos === false) {
      showToast('Marque ao menos um site, ou "Todos os sites"', true);
      return;
    }
    const dvrUser  = document.getElementById('zbxDvrUser')?.value.trim();
    const dvrPass  = document.getElementById('zbxDvrPass')?.value;
    const tgAuto   = document.getElementById('zbxTgAuto')?.checked;
    const tgToken  = document.getElementById('zbxTgToken')?.value.trim();
    const tgChat   = document.getElementById('zbxTgChat')?.value.trim();

    // A senha fica salva no servidor e o campo aparece vazio e travado. Exigir
    // senha digitada aqui impedia sincronizar mesmo com tudo configurado.
    if (!url || !user || (!pass && !window._zbxTemSenhaSalva)) {
      showToast('Preencha URL, usuario e senha do Zabbix', true);
      return;
    }

    const log    = document.getElementById('zabbixLog');
    const badge  = document.getElementById('zbxStatusBadge');
    const btn    = document.getElementById('btnGenZabbix');

    log.textContent = 'Conectando ao Zabbix\n';
    badge.style.display = 'none';
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-circle"></i> Sincronizando';
    lucide.createIcons();

    const payload = {
      url, user, pass, source, group, template,
      inv_mode: invMode,
      ...(site && { site }),
      ...(sites.length > 1 && { sites }),
      ...(dvrUser && { dvr_user: dvrUser }),
      ...(dvrPass && { dvr_pass: dvrPass }),
      tg_auto: tgAuto || false,
      ...(tgToken && { tg_token: tgToken }),
      ...(tgChat  && { tg_chat:  tgChat  }),
      // mapa site -> chat; campo vazio desliga o alerta daquele site
      ...(typeof zbxChatsPorSite === 'function' && { tg_chat_by_site: zbxChatsPorSite() }),
    };

    const res  = await api('/api/scripts/zabbix', { method: 'POST', body: JSON.stringify(payload) });
    const data = await res?.json().catch(() => ({}));

    btn.disabled = false;
    btn.innerHTML = '<i data-lucide="refresh-cw"></i> Sincronizar com Zabbix';
    lucide.createIcons();

    if (!res?.ok || data?.error || data?.ok === false) {
      log.textContent = ' Erro: ' + (data?.error || data?.detail || `HTTP ${res?.status || 'falha'}`) + '\n\n' + (data?.stderr || '');
      badge.textContent = 'Erro';
      badge.style.background = 'var(--danger-soft)';
      badge.style.color = 'var(--danger)';
      badge.style.display = 'inline-block';
    } else {
      const output = data?.stdout || data?.result || JSON.stringify(data, null, 2) || 'Concluido.';
      log.textContent = output;
      badge.textContent = ' Sincronizado';
      badge.style.background = 'var(--primary-soft)';
      badge.style.color = 'var(--primary)';
      badge.style.display = 'inline-block';
      showToast('Sincronizacao Zabbix concluida!');
    }
  });

  // Varredura OLT (via inventario)
  document.getElementById('btnScanOlt')?.addEventListener('click', openScanModal);

  //  Manutencao Cameras 
  document.getElementById('btnMntCamRefresh')?.addEventListener('click', () => { _mntCamAll = []; loadMntCam(); });
  document.getElementById('btnMntCamReboot')?.addEventListener('click', () => _mntCamRunAction('reboot'));
  document.getElementById('btnMntCamSnapshot')?.addEventListener('click', () => _mntCamRunAction('snapshot_force'));
  document.getElementById('btnMntCamTest')?.addEventListener('click', () => _mntCamRunAction('test'));
  document.getElementById('btnMntCamRename')?.addEventListener('click', openMntRenameModal);
  document.getElementById('btnMntCamTimeCheck')?.addEventListener('click', () => _mntCamRunAction('time_check'));
  document.getElementById('btnMntCamDayNight')?.addEventListener('click', openMntDayNightModal);
  document.getElementById('btnMntCamMirror')?.addEventListener('click', openMntMirrorModal);
  document.getElementById('btnMntCamQuality')?.addEventListener('click', openMntQualityModal);
  document.getElementById('btnMntCamNtp')?.addEventListener('click', openMntNtpModal);
  document.getElementById('closeMntNtp')?.addEventListener('click', closeMntNtpModal);
  document.getElementById('cancelMntNtp')?.addEventListener('click', closeMntNtpModal);
  document.getElementById('confirmMntNtp')?.addEventListener('click', runMntNtp);
  document.getElementById('mntNtpAddress')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') runMntNtp();
  });
  document.getElementById('btnMntCamNetwork')?.addEventListener('click', openMntNetworkModal);
  document.getElementById('btnMntCamShiftIp')?.addEventListener('click', openMntShiftIpModal);
  document.getElementById('btnMntCamPass')?.addEventListener('click', () => {
    const ips = [...document.querySelectorAll('.chk-mnt-cam:checked')].map(c => c.value);
    if (!ips.length) { showToast('Selecione ao menos uma camera', true); return; }
    document.getElementById('modalTrocarSenha')?.classList.remove('hidden');
    lucide.createIcons();
  });
  document.getElementById('mntStreamClose')?.addEventListener('click', closeMntStream);
  document.getElementById('btnMntCamSelectAll')?.addEventListener('click', () => {
    document.querySelectorAll('.chk-mnt-cam').forEach(c => { c.checked = true; c.closest('.mnt-cam-card')?.classList.add('selected'); });
    _mntCamUpdateCount();
  });
  document.getElementById('btnMntCamDeselect')?.addEventListener('click', () => {
    document.querySelectorAll('.chk-mnt-cam').forEach(c => { c.checked = false; c.closest('.mnt-cam-card')?.classList.remove('selected'); });
    _mntCamUpdateCount();
  });
  const mntCamSearch = document.getElementById('mntCamSearch');
  const runMntCamSearch = () => {
    _mntCamFilter.q = mntCamSearch?.value || '';
    _mntCamRender();
  };
  mntCamSearch?.addEventListener('input', runMntCamSearch);
  mntCamSearch?.addEventListener('keyup', runMntCamSearch);
  mntCamSearch?.addEventListener('search', runMntCamSearch);
  mntCamSearch?.addEventListener('change', runMntCamSearch);
  document.getElementById('mntCamSite')?.addEventListener('change', e => { _mntCamFilter.site = e.target.value; _mntCamRender(); });
  document.getElementById('mntCamView')?.addEventListener('change', e => setMntCamView(e.target.value));
  document.querySelectorAll('[data-mnt-status]').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('[data-mnt-status]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      _mntCamFilter.status = btn.dataset.mntStatus;
      _mntCamRender();
    });
  });

  //  Manutencao DVR 
  document.getElementById('btnMntDvrRefresh')?.addEventListener('click', loadMntDvr);
  document.getElementById('btnMntDvrReboot')?.addEventListener('click', () => _mntDvrRunAction('reboot'));
  document.getElementById('btnMntDvrNtp')?.addEventListener('click', () => _mntDvrRunAction('ntp'));
  document.getElementById('btnMntDvrSelectAll')?.addEventListener('click', () => {
    document.querySelectorAll('.chk-mnt-dvr').forEach(c => c.checked = true);
    _mntDvrUpdateCount();
  });

  //  Manutencao NVR 
  document.getElementById('btnMntNvrRefresh')?.addEventListener('click', loadMntNvr);
  document.getElementById('btnMntNvrReboot')?.addEventListener('click', () => _mntNvrRunAction('reboot'));
  document.getElementById('btnMntNvrNtp')?.addEventListener('click', () => _mntNvrRunAction('ntp'));
  document.getElementById('btnMntNvrSelectAll')?.addEventListener('click', () => {
    document.querySelectorAll('.chk-mnt-nvr').forEach(c => c.checked = true);
    _mntNvrUpdateCount();
  });

  // Auto-login via cookie HttpOnly.
  (async () => {
    const profile = await apiJson('/api/auth/me', { skipLogout: true });
    if (profile) showApp();
    else showLoginScreen();
  })();
});
