// Transcendence — mt5_page.dart
// Tab "Jurnal MT5": jurnal trading OTOMATIS dari akun MT5 Exness lewat
// backend Flask (mt5linux) di VPS. Backend read-only — halaman ini tidak
// pernah mengirim order.
//
// Gaya UI: Obsidian Precision (ui_kit.dart) — konsisten dengan tab lain.
// Fitur halaman ini TIDAK menyentuh kalkulator/engine sama sekali.
import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'engine.dart' show shortIdr;
import 'mt5_api.dart';
import 'mt5_filters.dart';
import 'mt5_models.dart';
import 'ui_kit.dart';

const _kPrefUrl = 'mt5_base_url';
const _kPrefToken = 'mt5_api_token';

class Mt5JournalPage extends StatefulWidget {
  final Mt5Api? api; // injeksi untuk test
  const Mt5JournalPage({super.key, this.api});

  @override
  State<Mt5JournalPage> createState() => _Mt5JournalPageState();
}

enum _Phase { boot, unconfigured, loading, ready, error }

class _Mt5JournalPageState extends State<Mt5JournalPage> {
  _Phase _phase = _Phase.boot;
  String? _error;

  Mt5Api? _api;
  String _baseUrl = '';
  String _token = '';

  Mt5Status? _status;
  Mt5Account? _account;
  Mt5Stats _stats = Mt5Stats.empty;
  Mt5Equity _equity = Mt5Equity.empty;
  List<Mt5Trade> _trades = [];
  int _page = 1;
  int _pages = 1;
  bool _loadingMore = false;
  bool _syncing = false;
  List<String> _symbols = [];

  // filter — Phase 1B: preset rolling/kalender/custom + zona tampilan
  DatePreset _preset = DatePreset.d30;
  AppTimeZone _tz = AppTimeZone.jakarta;
  DateTime? _customFrom, _customTo; // dinding-waktu di zona _tz
  String _symbolSel = 'SEMUA';

  @override
  void initState() {
    super.initState();
    _boot();
  }

  Future<void> _boot() async {
    final sp = await SharedPreferences.getInstance();
    if (!mounted) return;
    setState(() {
      _baseUrl = sp.getString(_kPrefUrl) ?? '';
      _token = sp.getString(_kPrefToken) ?? '';
      _api = widget.api ?? Mt5Api(baseUrl: _baseUrl, token: _token);
    });
    if (!(_api?.isConfigured ?? false)) {
      setState(() => _phase = _Phase.unconfigured);
      return;
    }
    await _refreshAll();
  }

  /// Rentang aktif dari preset + zona waktu. Null hanya jika preset
  /// custom belum lengkap/invalid — pemanggil menampilkan pesan.
  Mt5DateRange? _range() => rangeFor(
    _preset,
    _tz,
    DateTime.now().toUtc(),
    customStart: _customFrom,
    customEnd: _customTo,
  );

  String _cur() => _account?.currency ?? 'USD';

  // ------------------------------------------------------------ pemuatan
  Future<void> _refreshAll() async {
    final api = _api;
    if (api == null || !api.isConfigured) return;
    setState(() {
      _phase = _Phase.loading;
      _error = null;
    });
    try {
      final range = _range();
      if (range == null) {
        if (!mounted) return;
        setState(() => _phase = _Phase.ready);
        _toast('Rentang tanggal belum lengkap/valid.', err: true);
        return;
      }
      final sym = _symbolSel == 'SEMUA' ? null : _symbolSel;
      final qs = (from: range.fromUtc, to: range.toUtc, sym: sym);
      final rs = await Future.wait([
        api.status(),
        api.account().catchError(
          (_) => const Mt5Account(login: 0, server: '-', currency: 'USD'),
        ),
        api.stats(from: qs.from, to: qs.to, toExclusive: true, symbol: qs.sym),
        api.equityCurve(
          from: qs.from,
          to: qs.to,
          toExclusive: true,
          symbol: qs.sym,
        ),
        api.trades(
          page: 1,
          from: qs.from,
          to: qs.to,
          toExclusive: true,
          symbol: qs.sym,
        ),
      ]);
      List<String> syms = [];
      try {
        syms = await api.symbols();
      } on Mt5ApiError {
        syms = [];
      }
      final tl = rs[4] as Mt5TradeList;
      if (!mounted) return;
      setState(() {
        _status = rs[0] as Mt5Status;
        _account = rs[1] as Mt5Account;
        _stats = rs[2] as Mt5Stats;
        _equity = rs[3] as Mt5Equity;
        _trades = tl.items;
        _page = 1;
        _pages = tl.pages;
        _symbols = syms;
        if (_symbolSel != 'SEMUA' &&
            syms.isNotEmpty &&
            !syms.contains(_symbolSel)) {
          _symbolSel = 'SEMUA';
        }
        _phase = _Phase.ready;
      });
    } on Mt5ApiError catch (e) {
      if (!mounted) return;
      setState(() {
        _phase = _Phase.error;
        _error = e.friendly;
      });
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _phase = _Phase.error;
        _error = 'Gagal memuat data jurnal.';
      });
    }
  }

  Future<void> _loadMore() async {
    final api = _api;
    if (api == null || _loadingMore || _page >= _pages) return;
    final range = _range();
    if (range == null) return;
    setState(() => _loadingMore = true);
    try {
      final tl = await api.trades(
        page: _page + 1,
        from: range.fromUtc,
        to: range.toUtc,
        toExclusive: true,
        symbol: _symbolSel == 'SEMUA' ? null : _symbolSel,
      );
      if (!mounted) return;
      setState(() {
        _trades = [..._trades, ...tl.items];
        _page = tl.page;
        _pages = tl.pages;
      });
    } on Mt5ApiError catch (e) {
      _toast(e.friendly, err: true);
    } finally {
      if (mounted) setState(() => _loadingMore = false);
    }
  }

  Future<void> _syncNow({bool full = false}) async {
    final api = _api;
    if (api == null || _syncing) return;
    setState(() => _syncing = true);
    try {
      final s = await api.triggerSync(full: full);
      if (!mounted) return;
      final st = (s['status'] ?? '?') as String;
      if (st == 'ok') {
        _toast(
          'Sinkron ok — '
          '${s['deals_upserted'] ?? 0} deal, '
          '${s['trades_updated'] ?? 0} trade.',
        );
      } else {
        _toast('Sinkron gagal: ${s['error'] ?? st}', err: true);
      }
      await _refreshAll();
    } on Mt5ApiError catch (e) {
      _toast(e.friendly, err: true);
    } finally {
      if (mounted) setState(() => _syncing = false);
    }
  }

  void _toast(String msg, {bool err = false}) {
    if (!mounted) return;
    final p = Palette.of(context);
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(
          msg,
          style: TextStyle(
            fontFamily: kBodyFont,
            fontSize: 13,
            color: err ? p.onRed : p.onMint,
          ),
        ),
        backgroundColor: err ? p.redSolid : p.mintSolid,
        behavior: SnackBarBehavior.floating,
        duration: const Duration(seconds: 3),
      ),
    );
  }

  // ------------------------------------------------------------- setting
  Future<void> _openSettings() async {
    final urlC = TextEditingController(text: _baseUrl);
    final tokC = TextEditingController(text: _token);
    final ok = await showModalBottomSheet<bool>(
      context: context,
      isScrollControlled: true,
      builder: (ctx) {
        final p = Palette.of(ctx);
        return Padding(
          padding: EdgeInsets.only(
            left: 16,
            right: 16,
            top: 16,
            bottom: MediaQuery.of(ctx).viewInsets.bottom + 24,
          ),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Icon(Icons.dns, size: 17, color: p.gold),
                  const SizedBox(width: 8),
                  Text(
                    'Server Jurnal MT5',
                    style: txt(ctx, 15, w: FontWeight.w600),
                  ),
                ],
              ),
              const SizedBox(height: 4),
              Text(
                'Alamat backend Flask di VPS (HTTPS) + API token. '
                'Ambil token dari file .env backend (API_TOKEN).',
                style: txt(ctx, 12, color: p.muted),
              ),
              const SizedBox(height: 14),
              _textField(
                ctx,
                'URL server',
                'https://jurnal.domain.com',
                urlC,
                false,
              ),
              const SizedBox(height: 10),
              _textField(ctx, 'API token', 'xxxxxxxx...', tokC, true),
              const SizedBox(height: 16),
              GoldButton(
                'SIMPAN',
                icon: Icons.save_outlined,
                onTap: () => Navigator.pop(ctx, true),
              ),
            ],
          ),
        );
      },
    );
    if (ok != true) return;
    final sp = await SharedPreferences.getInstance();
    await sp.setString(_kPrefUrl, urlC.text.trim());
    await sp.setString(_kPrefToken, tokC.text.trim());
    if (!mounted) return;
    setState(() {
      _baseUrl = urlC.text.trim();
      _token = tokC.text.trim();
      _api = widget.api ?? Mt5Api(baseUrl: _baseUrl, token: _token);
    });
    await _refreshAll();
  }

  Widget _textField(
    BuildContext ctx,
    String label,
    String hint,
    TextEditingController c,
    bool obscure,
  ) {
    final p = Palette.of(ctx);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(label.toUpperCase(), style: labelSm(ctx, color: p.dim)),
        const SizedBox(height: 6),
        Container(
          decoration: BoxDecoration(
            color: p.surface1,
            borderRadius: BorderRadius.circular(10),
            border: Border.all(width: 1, color: p.line),
          ),
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
          child: TextField(
            controller: c,
            obscureText: obscure,
            autocorrect: false,
            enableSuggestions: false,
            style: mono(ctx, 14, w: FontWeight.w600),
            decoration: InputDecoration(
              isDense: true,
              border: InputBorder.none,
              hintText: hint,
              hintStyle: mono(ctx, 13, color: p.dim),
            ),
            cursorColor: p.goldSolid,
          ),
        ),
      ],
    );
  }

  // ---------------------------------------------------------------- build
  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    switch (_phase) {
      case _Phase.boot:
      case _Phase.loading:
        return const Center(child: CircularProgressIndicator(strokeWidth: 2));
      case _Phase.unconfigured:
        return _unconfigured(p);
      case _Phase.error:
        return _errorView(p);
      case _Phase.ready:
        return RefreshIndicator(
          color: p.goldSolid,
          onRefresh: _refreshAll,
          child: SingleChildScrollView(
            physics: const AlwaysScrollableScrollPhysics(),
            padding: const EdgeInsets.fromLTRB(16, 16, 16, 24),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                SectionHead(Icons.cloud_sync_outlined, 'Jurnal MT5 Otomatis'),
                const SizedBox(height: 4),
                Text(
                  'Riwayat trade akun MT5 Exness tersinkron otomatis '
                  'dari VPS — read-only, tanpa eksekusi order.',
                  style: txt(context, 12, color: p.muted),
                ),
                const SizedBox(height: 14),
                _statusCard(p),
                const SizedBox(height: 10),
                _accountCard(p),
                const SizedBox(height: 10),
                _filterRow(p),
                const SizedBox(height: 10),
                _statsGrid(p),
                const SizedBox(height: 10),
                _equityCard(p),
                const SizedBox(height: 16),
                SectionHead(
                  Icons.receipt_long,
                  'Daftar Trade',
                  trailing: BadgePill(
                    '${_stats.totalClosed} closed',
                    color: p.warm,
                    bg: p.cardHi,
                  ),
                ),
                const SizedBox(height: 10),
                ..._trades.map(_tradeTile),
                if (_page < _pages)
                  Padding(
                    padding: const EdgeInsets.only(top: 8),
                    child: GhostButton(
                      _loadingMore ? 'MEMUAT…' : 'MUAT LAGI',
                      icon: Icons.expand_more,
                      expanded: true,
                      onTap: _loadMore,
                    ),
                  ),
                if (_trades.isEmpty)
                  ObsCard(
                    children: [
                      Text(
                        'Belum ada trade pada rentang ini.',
                        style: txt(context, 13, color: p.muted),
                      ),
                    ],
                  ),
              ],
            ),
          ),
        );
    }
  }

  Widget _unconfigured(Palette p) {
    return SingleChildScrollView(
      padding: const EdgeInsets.fromLTRB(16, 16, 16, 24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SectionHead(Icons.cloud_sync_outlined, 'Jurnal MT5 Otomatis'),
          const SizedBox(height: 4),
          Text(
            'Riwayat trade akun MT5 Exness, tersinkron otomatis dari VPS.',
            style: txt(context, 12, color: p.muted),
          ),
          const SizedBox(height: 14),
          ObsCard(
            children: [
              Row(
                children: [
                  Icon(Icons.dns, size: 17, color: p.gold),
                  const SizedBox(width: 8),
                  Text(
                    'BELUM TERHUBUNG',
                    style: labelSm(context, color: p.muted),
                  ),
                ],
              ),
              const SizedBox(height: 10),
              Text(
                'Isi alamat backend dan API token untuk mulai memuat jurnal. '
                'Backend berjalan di VPS Ubuntu dengan Wine + mt5linux '
                '(lihat docs/MT5-UBUNTU-NO-DOCKER.md).',
                style: txt(context, 13, color: p.muted),
              ),
              const SizedBox(height: 14),
              GhostButton(
                'ATUR SERVER',
                icon: Icons.settings_outlined,
                expanded: true,
                onTap: _openSettings,
              ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _errorView(Palette p) {
    return SingleChildScrollView(
      padding: const EdgeInsets.fromLTRB(16, 16, 16, 24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SectionHead(Icons.cloud_sync_outlined, 'Jurnal MT5 Otomatis'),
          const SizedBox(height: 14),
          ObsCard(
            children: [
              Row(
                children: [
                  Icon(Icons.cloud_off, size: 17, color: p.red),
                  const SizedBox(width: 8),
                  Text(
                    'GAGAL TERHUBUNG',
                    style: labelSm(context, color: p.muted),
                  ),
                ],
              ),
              const SizedBox(height: 8),
              Text(_error ?? '-', style: txt(context, 13)),
              const SizedBox(height: 14),
              Row(
                children: [
                  Expanded(
                    child: GhostButton(
                      'COBA LAGI',
                      icon: Icons.refresh,
                      expanded: true,
                      onTap: _refreshAll,
                    ),
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: GhostButton(
                      'SERVER',
                      icon: Icons.dns,
                      expanded: true,
                      onTap: _openSettings,
                    ),
                  ),
                ],
              ),
            ],
          ),
        ],
      ),
    );
  }

  // -------------------------------------------------------------- kartu
  Widget _statusCard(Palette p) {
    final st = _status;
    final conn = st?.connected ?? false;
    final last = st?.lastSync?['last_sync_finished_at'] as String?;
    final reason = st?.reason;
    return ObsCard(
      children: [
        Row(
          children: [
            PulseDot(conn ? p.mint : p.red, size: 8),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                conn ? 'TERHUBUNG' : 'TERPUTUS',
                style: txt(
                  context,
                  14,
                  w: FontWeight.w600,
                  color: conn ? p.mint : p.red,
                ),
              ),
            ),
            GhostButton(
              _syncing ? 'SINKRON…' : 'SINKRON',
              icon: Icons.sync,
              onTap: _syncing ? null : _syncNow,
            ),
          ],
        ),
        const SizedBox(height: 8),
        Text(
          reason == 'disabled'
              ? 'Sinkronisasi dimatikan di backend (MT5_ENABLED=false).'
              : conn
              ? 'Provider ${st?.provider ?? '-'} · sinkron terakhir: '
                    '${_fmtIso(last) ?? 'belum ada'}'
              : (reason ?? 'Status belum diketahui.'),
          style: txt(context, 12, color: p.muted),
        ),
        const SizedBox(height: 10),
        Row(
          children: [
            Expanded(
              child: GhostButton(
                'SERVER',
                icon: Icons.dns,
                onTap: _openSettings,
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: GhostButton(
                'SINKRON PENUH',
                icon: Icons.history,
                onTap: _syncing ? null : () => _syncNow(full: true),
              ),
            ),
          ],
        ),
      ],
    );
  }

  Widget _accountCard(Palette p) {
    final a = _account;
    return ObsCard(
      children: [
        Row(
          children: [
            Icon(Icons.account_balance, size: 15, color: p.gold),
            const SizedBox(width: 7),
            Text('AKUN', style: labelSm(context, color: p.muted)),
            const Spacer(),
            BadgePill('#${a?.login ?? '-'}', color: p.gold, bg: p.goldsoft),
          ],
        ),
        const SizedBox(height: 12),
        Row(
          children: [
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('SERVER', style: labelSm(context, color: p.dim)),
                  const SizedBox(height: 3),
                  Text(
                    a?.server ?? '-',
                    style: mono(context, 13, w: FontWeight.w600),
                  ),
                ],
              ),
            ),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('BROKER', style: labelSm(context, color: p.dim)),
                  const SizedBox(height: 3),
                  Text(
                    a?.company ?? '-',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: mono(context, 13, w: FontWeight.w600),
                  ),
                ],
              ),
            ),
          ],
        ),
        const SizedBox(height: 10),
        Row(
          children: [
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('BALANCE', style: labelSm(context, color: p.dim)),
                  const SizedBox(height: 3),
                  Text(
                    '${shortIdr(a?.balance ?? 0)} ${_cur()}',
                    style: mono(context, 15, w: FontWeight.w600, color: p.ink),
                  ),
                ],
              ),
            ),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('EQUITY', style: labelSm(context, color: p.dim)),
                  const SizedBox(height: 3),
                  Text(
                    '${shortIdr(a?.equity ?? 0)} ${_cur()}',
                    style: mono(context, 15, w: FontWeight.w600, color: p.mint),
                  ),
                ],
              ),
            ),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('LEVERAGE', style: labelSm(context, color: p.dim)),
                  const SizedBox(height: 3),
                  Text(
                    '1:${a?.leverage ?? '-'}',
                    style: mono(context, 15, w: FontWeight.w600, color: p.warm),
                  ),
                ],
              ),
            ),
          ],
        ),
      ],
    );
  }

  Widget _filterRow(Palette p) {
    final range = _range();
    final tzShort = _tz.id == 'UTC' ? 'UTC' : 'WIB';
    final intervalText = range == null
        ? 'Rentang belum dipilih'
        : range.fromUtc == null
        ? range.label
        : '${range.label} · ${fmtDayMonth(range.fromUtc, _tz)} – '
              '${fmtDayMonth(range.toUtc, _tz)} (batas akhir eksklusif)';
    return Column(
      children: [
        SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          clipBehavior: Clip.none,
          child: Row(
            children: [
              for (final preset in DatePreset.values) ...[
                PresetChip(
                  preset.label,
                  active: _preset == preset,
                  expanded: false, // di dalam scroll horizontal
                  onTap: () => _pickPreset(preset),
                ),
                const SizedBox(width: 6),
              ],
            ],
          ),
        ),
        const SizedBox(height: 8),
        Row(
          children: [
            GhostButton(
              _symbolSel,
              icon: Icons.candlestick_chart,
              onTap: _pickSymbol,
            ),
            const SizedBox(width: 6),
            GhostButton(tzShort, icon: Icons.schedule_outlined, onTap: _pickTz),
          ],
        ),
        const SizedBox(height: 8),
        Row(
          children: [
            Icon(Icons.date_range, size: 12, color: p.dim),
            const SizedBox(width: 5),
            Expanded(
              child: Text(
                intervalText,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: mono(context, 11, color: p.warm),
              ),
            ),
          ],
        ),
        const SizedBox(height: 8),
        Row(
          children: [
            BadgePill(
              'winrate ${_stats.winRate?.toStringAsFixed(1) ?? '-'}%',
              color: p.gold,
              bg: p.goldsoft,
            ),
          ],
        ),
      ],
    );
  }

  Future<void> _pickPreset(DatePreset preset) async {
    if (preset != DatePreset.custom) {
      setState(() => _preset = preset);
      await _refreshAll();
      return;
    }
    final nowLocal = _tz.utcToLocal(DateTime.now().toUtc());
    final start = await showDatePicker(
      context: context,
      initialDate: _customFrom ?? nowLocal,
      firstDate: DateTime(2000),
      lastDate: nowLocal,
      helpText: 'Tanggal MULAI (zona $_tz)',
    );
    if (start == null || !mounted) return;
    final end = await showDatePicker(
      context: context,
      initialDate: _customTo ?? start,
      firstDate: start,
      lastDate: nowLocal,
      helpText: 'Tanggal AKHIR (zona $_tz)',
    );
    if (end == null || !mounted) return;
    setState(() {
      _customFrom = start;
      _customTo = end;
      _preset = DatePreset.custom;
    });
    await _refreshAll();
  }

  Future<void> _pickTz() async {
    final sel = await showModalBottomSheet<AppTimeZone>(
      context: context,
      builder: (ctx) {
        final p = Palette.of(ctx);
        return SafeArea(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const SizedBox(height: 12),
              Text('ZONA WAKTU TAMPILAN', style: labelSm(ctx, color: p.muted)),
              const SizedBox(height: 4),
              Padding(
                padding: const EdgeInsets.symmetric(
                  horizontal: 16,
                  vertical: 4,
                ),
                child: Text(
                  'Batas tanggal dikonversi ke UTC sebelum dikirim ke '
                  'server. Waktu broker MT5 bisa berbeda — lihat '
                  'dokumen rekonsiliasi.',
                  style: txt(ctx, 11, color: p.dim),
                ),
              ),
              ...AppTimeZone.supported.map(
                (z) => ListTile(
                  dense: true,
                  title: Text(
                    z.label,
                    style: mono(ctx, 14, w: FontWeight.w600),
                  ),
                  subtitle: Text(z.id, style: txt(ctx, 11, color: p.dim)),
                  trailing: z.id == _tz.id
                      ? Icon(Icons.check, size: 18, color: p.gold)
                      : null,
                  onTap: () => Navigator.pop(ctx, z),
                ),
              ),
              const SizedBox(height: 8),
            ],
          ),
        );
      },
    );
    if (sel == null || sel.id == _tz.id) return;
    setState(() => _tz = sel);
    await _refreshAll();
  }

  Future<void> _pickSymbol() async {
    final syms = ['SEMUA', ..._symbols];
    final sel = await showModalBottomSheet<String>(
      context: context,
      builder: (ctx) {
        final p = Palette.of(ctx);
        return SafeArea(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const SizedBox(height: 12),
              Text('PILIH SIMBOL', style: labelSm(ctx, color: p.muted)),
              const SizedBox(height: 6),
              ...syms.map(
                (s) => ListTile(
                  dense: true,
                  title: Text(s, style: mono(ctx, 14, w: FontWeight.w600)),
                  trailing: s == _symbolSel
                      ? Icon(Icons.check, size: 18, color: p.gold)
                      : null,
                  onTap: () => Navigator.pop(ctx, s),
                ),
              ),
              const SizedBox(height: 8),
            ],
          ),
        );
      },
    );
    if (sel == null || sel == _symbolSel) return;
    setState(() => _symbolSel = sel);
    await _refreshAll();
  }

  Widget _cell(String label, String val, Color c) {
    final p = Palette.of(context);
    return Expanded(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(label, style: labelSm(context, color: p.dim)),
          const SizedBox(height: 4),
          Text(
            val,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: mono(context, 14, w: FontWeight.w600, color: c),
          ),
        ],
      ),
    );
  }

  Widget _statsGrid(Palette p) {
    final s = _stats;
    final cur = _cur();
    String money(double v) => '${v >= 0 ? '+' : '−'}${shortIdr(v.abs())} $cur';
    return ObsCard(
      children: [
        Row(
          children: [
            Icon(Icons.query_stats, size: 15, color: p.gold),
            const SizedBox(width: 7),
            Text('STATISTIK', style: labelSm(context, color: p.muted)),
            const Spacer(),
            BadgePill(
              '${s.totalClosed} closed · ${s.openTrades} open',
              color: p.muted,
              bg: p.cardHi,
            ),
          ],
        ),
        const SizedBox(height: 12),
        Row(
          children: [
            _cell(
              'WINRATE',
              '${s.winRate?.toStringAsFixed(1).replaceFirst('.', ',') ?? '-'}%',
              p.gold,
            ),
            const SizedBox(width: 12),
            _cell(
              'NET P/L',
              money(s.netProfit),
              s.netProfit >= 0 ? p.mint : p.red,
            ),
            const SizedBox(width: 12),
            _cell(
              'PROFIT FACTOR',
              s.profitFactor?.toStringAsFixed(2) ?? 'N/A',
              s.profitFactor != null ? p.mint : p.dim,
            ),
          ],
        ),
        const SizedBox(height: 10),
        Row(
          children: [
            _cell('MENANG / KALAH', '${s.wins} / ${s.losses}', p.ink),
            const SizedBox(width: 12),
            _cell(
              'AVG WIN',
              s.averageWin != null ? '+${shortIdr(s.averageWin!)}' : '-',
              p.mint,
            ),
            const SizedBox(width: 12),
            _cell(
              'AVG LOSS',
              s.averageLoss != null ? '−${shortIdr(s.averageLoss!)}' : '-',
              p.red,
            ),
          ],
        ),
        const SizedBox(height: 10),
        Row(
          children: [
            _cell(
              'EXPECTANCY',
              s.expectancy != null ? money(s.expectancy!) : '-',
              p.ink,
            ),
            const SizedBox(width: 12),
            _cell('VOLUME', shortIdr(s.totalVolume), p.warm),
            const SizedBox(width: 12),
            _cell(
              'MAX DD',
              s.totalClosed == 0 ? '-' : '−${shortIdr(_equity.maxDrawdown)}',
              p.red,
            ),
          ],
        ),
      ],
    );
  }

  Widget _equityCard(Palette p) {
    final pts = _equity.points;
    return ObsCard(
      children: [
        Row(
          children: [
            Icon(Icons.show_chart, size: 15, color: p.gold),
            const SizedBox(width: 7),
            Text(
              'KURVA P/L TRADING (NET)',
              style: labelSm(context, color: p.muted),
            ),
            const Spacer(),
            BadgePill(
              'DD ${_equity.maxDrawdownPct.toStringAsFixed(1)}%',
              color: _equity.maxDrawdownPct > 0 ? p.red : p.dim,
              bg: p.redsoft,
            ),
          ],
        ),
        const SizedBox(height: 12),
        SizedBox(
          height: 130,
          width: double.infinity,
          child: pts.length < 2
              ? Center(
                  child: Text(
                    'Data kurang untuk menggambar kurva.',
                    style: txt(context, 12, color: p.dim),
                  ),
                )
              : CustomPaint(painter: _EquityPainter(p, pts)),
        ),
        const SizedBox(height: 6),
        Text(
          'Kumulatif P/L trading — BUKAN kurva equity akun historis '
          '(snapshot equity tidak tersedia dari riwayat MT5).',
          style: txt(context, 10, color: p.dim),
        ),
        const SizedBox(height: 4),
        Row(
          children: [
            Text('Net kumulatif: ', style: txt(context, 11, color: p.dim)),
            Text(
              '${_equity.finalNetCum >= 0 ? '+' : '−'}'
              '${shortIdr(_equity.finalNetCum.abs())} ${_cur()}',
              style: mono(
                context,
                12,
                w: FontWeight.w600,
                color: _equity.finalNetCum >= 0 ? p.mint : p.red,
              ),
            ),
            const SizedBox(width: 12),
            Text('Deposit/WD: ', style: txt(context, 11, color: p.dim)),
            Text(
              '${shortIdr(_equity.balanceOpsCum)} ${_cur()}',
              style: mono(context, 12, w: FontWeight.w600, color: p.warm),
            ),
          ],
        ),
      ],
    );
  }

  Widget _tradeTile(Mt5Trade t) {
    final p = Palette.of(context);
    final win = t.isWin;
    final pnlC = t.netProfit > 0 ? p.mint : (t.netProfit < 0 ? p.red : p.dim);
    final dirC = t.direction == 'buy' ? p.mint : p.red;
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Material(
        color: p.card,
        borderRadius: BorderRadius.circular(12),
        child: InkWell(
          borderRadius: BorderRadius.circular(12),
          onTap: () => _openDetail(t),
          child: Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(12),
              border: Border.all(width: 1, color: p.lineSoft),
            ),
            child: Row(
              children: [
                Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        BadgePill(
                          (t.direction ?? '?').toUpperCase(),
                          color: dirC,
                          bg: dirC == p.mint ? p.goodsoft : p.redsoft,
                        ),
                        const SizedBox(width: 6),
                        Text(
                          t.symbol,
                          style: mono(context, 13, w: FontWeight.w600),
                        ),
                        if (t.isOpen) ...[
                          const SizedBox(width: 6),
                          BadgePill('OPEN', color: p.gold, bg: p.goldsoft),
                        ],
                      ],
                    ),
                    const SizedBox(height: 4),
                    Text(
                      '${_fmtIso(t.closeTime ?? t.openTime)} · '
                      '${shortIdr(t.volumeOut)}/'
                      '${shortIdr(t.volumeIn)} lot'
                      '${t.pips != null ? ' · ${t.pips! >= 0 ? '+' : '−'}'
                                '${t.pips!.abs().toStringAsFixed(1)} pip' : ''}',
                      style: txt(context, 11, color: p.muted),
                    ),
                    if (t.exitReason != null || t.rr != null) ...[
                      const SizedBox(height: 4),
                      Row(
                        children: [
                          if (t.exitReason != null)
                            BadgePill(
                              t.exitReason!.toUpperCase(),
                              color: t.exitReason == 'tp' ? p.mint : p.red,
                              bg: t.exitReason == 'tp' ? p.goodsoft : p.redsoft,
                            ),
                          if (t.exitReason != null && t.rr != null)
                            const SizedBox(width: 6),
                          if (t.rr != null)
                            BadgePill(
                              'RR ${t.rr!.toStringAsFixed(2)}',
                              color: p.warm,
                              bg: p.cardHi,
                            ),
                        ],
                      ),
                    ],
                  ],
                ),
                const Spacer(),
                Column(
                  crossAxisAlignment: CrossAxisAlignment.end,
                  children: [
                    Text(
                      '${t.netProfit >= 0 ? '+' : '−'}'
                      '${shortIdr(t.netProfit.abs())} ${_cur()}',
                      style: mono(context, 14, w: FontWeight.w600, color: pnlC),
                    ),
                    const SizedBox(height: 4),
                    Text(
                      t.status == 'open'
                          ? 'berjalan'
                          : (win ? 'menang' : 'rugi'),
                      style: txt(
                        context,
                        10.5,
                        color: t.status == 'open' ? p.gold : p.dim,
                      ),
                    ),
                    const SizedBox(height: 4),
                    Icon(Icons.chevron_right, size: 16, color: p.dim),
                  ],
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Future<void> _openDetail(Mt5Trade t) async {
    final api = _api;
    if (api == null) return;
    Mt5TradeDetail? d;
    try {
      d = await api.tradeDetail(t.positionId);
    } on Mt5ApiError catch (e) {
      _toast(e.friendly, err: true);
      return;
    }
    if (!mounted) return;
    final p = Palette.of(context);
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      backgroundColor: p.surface1,
      builder: (ctx) => DraggableScrollableSheet(
        expand: false,
        initialChildSize: 0.75,
        maxChildSize: 0.95,
        builder: (ctx, sc) => SingleChildScrollView(
          controller: sc,
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  BadgePill(
                    (d!.trade.direction ?? '?').toUpperCase(),
                    color: d.trade.direction == 'buy' ? p.mint : p.red,
                    bg: p.cardHi,
                  ),
                  const SizedBox(width: 8),
                  Text(
                    '${d.trade.symbol} · #${d.trade.positionId}',
                    style: txt(ctx, 15, w: FontWeight.w600),
                  ),
                  const Spacer(),
                  if (d.trade.isOpen)
                    BadgePill('OPEN', color: p.gold, bg: p.goldsoft)
                  else
                    BadgePill(
                      (d.trade.exitReason ?? 'closed').toUpperCase(),
                      color: d.trade.exitReason == 'sl' ? p.red : p.mint,
                      bg: p.cardHi,
                    ),
                ],
              ),
              const SizedBox(height: 14),
              _kv(
                ctx,
                p,
                'Status',
                d.trade.status == 'open' ? 'Terbuka' : 'Tertutup',
              ),
              _kv(
                ctx,
                p,
                'Volume in / out',
                '${shortIdr(d.trade.volumeIn)} / ${shortIdr(d.trade.volumeOut)} lot',
              ),
              _kv(ctx, p, 'Entry (VWAP)', _px(d.trade.entryPrice)),
              _kv(
                ctx,
                p,
                'Exit (VWAP)',
                d.trade.exitPrice != null ? _px(d.trade.exitPrice) : '-',
              ),
              _kv(ctx, p, 'Dibuka', _fmtIso(d.trade.openTime) ?? '-'),
              _kv(ctx, p, 'Ditutup', _fmtIso(d.trade.closeTime) ?? '-'),
              _kv(
                ctx,
                p,
                'Pips',
                d.trade.pips != null
                    ? '${d.trade.pips! >= 0 ? '+' : '−'}'
                          '${d.trade.pips!.abs().toStringAsFixed(1)} '
                          '(jarak ${d.trade.pipDistance ?? '-'})'
                    : '-',
              ),
              _kv(ctx, p, 'Profit', '${shortIdr(d.trade.profit)} ${_cur()}'),
              _kv(ctx, p, 'Komisi', shortIdr(d.trade.commission)),
              _kv(ctx, p, 'Swap', shortIdr(d.trade.swap)),
              _kv(ctx, p, 'Fee', shortIdr(d.trade.fee)),
              _kv(
                ctx,
                p,
                'NET P/L',
                '${d.trade.netProfit >= 0 ? '+' : '−'}'
                    '${shortIdr(d.trade.netProfit.abs())} ${_cur()}',
                color: d.trade.netProfit >= 0 ? p.mint : p.red,
              ),
              // RR JUJUR: tanpa SL awal tampil N/A — tidak mengarang angka.
              _kv(
                ctx,
                p,
                'Risk:Reward',
                d.trade.rr != null
                    ? '1 : ${d.trade.rr!.toStringAsFixed(2)}'
                    : 'N/A',
              ),
              _kv(ctx, p, 'SL awal', _px(d.trade.slInitial)),
              _kv(ctx, p, 'TP awal', _px(d.trade.tpInitial)),
              if (d.trade.isOpen) ...[
                _kv(ctx, p, 'SL sekarang', _px(d.trade.slCurrent)),
                _kv(ctx, p, 'TP sekarang', _px(d.trade.tpCurrent)),
                _kv(ctx, p, 'Harga sekarang', _px(d.trade.priceCurrent)),
              ],
              _kv(
                ctx,
                p,
                'Magic / Comment',
                '${d.trade.magic ?? '-'} · ${d.trade.comment?.isEmpty ?? true ? '-' : d.trade.comment}',
              ),
              const SizedBox(height: 14),
              Text(
                'DEALS (${d.deals.length})',
                style: labelSm(ctx, color: p.muted),
              ),
              const SizedBox(height: 6),
              ...d.deals.map(
                (x) => Container(
                  width: double.infinity,
                  margin: const EdgeInsets.only(bottom: 6),
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(
                    color: p.card,
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(width: 1, color: p.lineSoft),
                  ),
                  child: Text(
                    '#${x['ticket']} · ${x['type_name'] ?? '-'} '
                    '${x['entry_name'] ?? '-'} · '
                    '${x['volume']} @ ${x['price']} · '
                    'P/L ${x['profit']} · ${_fmtIso(x['time_utc'] as String?)}',
                    style: mono(ctx, 11.5, color: p.muted),
                  ),
                ),
              ),
              const SizedBox(height: 10),
            ],
          ),
        ),
      ),
    );
  }

  Widget _kv(BuildContext ctx, Palette p, String k, String v, {Color? color}) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 7),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 130,
            child: Text(k, style: txt(ctx, 12.5, color: p.muted)),
          ),
          Expanded(
            child: Text(
              v,
              textAlign: TextAlign.right,
              style: mono(ctx, 12.5, w: FontWeight.w600, color: color ?? p.ink),
            ),
          ),
        ],
      ),
    );
  }

  String _px(double? v) {
    if (v == null) return '-';
    final dec = v.abs() < 10 ? 5 : 2;
    return v.toStringAsFixed(dec);
  }

  String? _fmtIso(String? iso) {
    if (iso == null || iso.isEmpty) return null;
    final dt = DateTime.tryParse(iso.replaceFirst('Z', ''));
    if (dt == null) return iso;
    final l = dt.toLocal();
    return '${l.day.toString().padLeft(2, '0')}/'
        '${l.month.toString().padLeft(2, '0')} '
        '${l.hour.toString().padLeft(2, '0')}:'
        '${l.minute.toString().padLeft(2, '0')}';
  }
}

// ---------------------------------------------------------------------------
// Painter equity curve sederhana (garis kumulatif net P/L).
// ---------------------------------------------------------------------------

class _EquityPainter extends CustomPainter {
  final Palette p;
  final List<Mt5CurvePoint> pts;
  _EquityPainter(this.p, this.pts);

  @override
  void paint(Canvas canvas, Size size) {
    final vals = pts.map((e) => e.netCum).toList();
    double lo = vals.reduce((a, b) => a < b ? a : b);
    double hi = vals.reduce((a, b) => a > b ? a : b);
    if (lo > 0) lo = 0;
    if (hi < 0) hi = 0;
    final span = (hi - lo) == 0 ? 1.0 : (hi - lo);
    Offset xy(int i, double v) => Offset(
      size.width * i / (pts.length - 1),
      size.height * (1 - (v - lo) / span),
    );

    // baseline 0
    final zeroY = size.height * (1 - (0 - lo) / span);
    final base = Paint()
      ..color = p.line
      ..strokeWidth = 1;
    canvas.drawLine(Offset(0, zeroY), Offset(size.width, zeroY), base);

    // area di bawah kurva
    final path = Path()..moveTo(0, xy(0, vals[0]).dy);
    for (var i = 1; i < pts.length; i++) {
      path.lineTo(xy(i, vals[i]).dx, xy(i, vals[i]).dy);
    }
    path.lineTo(size.width, size.height);
    path.lineTo(0, size.height);
    path.close();
    canvas.drawPath(
      path,
      Paint()
        ..shader = LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: [p.mint.withAlpha(60), p.mint.withAlpha(0)],
        ).createShader(Rect.fromLTWH(0, 0, size.width, size.height)),
    );

    // garis kurva
    final line = Paint()
      ..color = p.mint
      ..strokeWidth = 2
      ..style = PaintingStyle.stroke
      ..strokeCap = StrokeCap.round;
    final lp = Path()..moveTo(xy(0, vals[0]).dx, xy(0, vals[0]).dy);
    for (var i = 1; i < pts.length; i++) {
      lp.lineTo(xy(i, vals[i]).dx, xy(i, vals[i]).dy);
    }
    canvas.drawPath(lp, line);

    // titik deposit / withdrawal
    for (var i = 0; i < pts.length; i++) {
      if (pts[i].type == 'trade') continue;
      canvas.drawCircle(xy(i, vals[i]), 3.2, Paint()..color = p.goldSolid);
    }
  }

  @override
  bool shouldRepaint(covariant _EquityPainter old) => old.pts != pts;
}
