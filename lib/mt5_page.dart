// Transcendence — mt5_page.dart
// Tab "Jurnal MT5": jurnal trading OTOMATIS dari akun MT5 Exness lewat
// backend Flask (mt5linux) di VPS. Backend read-only — halaman ini tidak
// pernah mengirim order.
//
// Gaya UI: Obsidian Precision (ui_kit.dart) — konsisten dengan tab lain.
// Fitur halaman ini TIDAK menyentuh kalkulator/engine sama sekali.
import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'engine.dart' show fmtLots, fmtMoney, one, two;
import 'mt5_api.dart';
import 'mt5_auth.dart';
import 'mt5_filters.dart';
import 'mt5_models.dart';
import 'ui_kit.dart';

const _kPrefUrl = 'mt5_base_url';

class Mt5JournalPage extends StatefulWidget {
  final Mt5Api? api; // injeksi untuk test
  const Mt5JournalPage({super.key, this.api});

  @override
  State<Mt5JournalPage> createState() => _Mt5JournalPageState();
}

enum _Phase { boot, unconfigured, loading, ready, invalidRange, error }

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

  // Phase 1.1 anti-race (Temuan 5): setiap refreshAll menaikkan _epoch.
  // Respons/append dari epoch lama yang selesai belakangan DIBUANG, jadi
  // yang tampil selalu milik filter yang diminta terakhir.
  int _epoch = 0;

  // Snapshot rentang milik fetch terakhir (label interval + halaman-2
  // _loadMore memakai ini, BUKAN menghitung ulang rangeFor yang berubah
  // seiring waktu — sumber race halaman-2 dari rentang lama).
  Mt5DateRange? _activeRange;

  @override
  void initState() {
    super.initState();
    _boot();
  }

  Future<void> _boot() async {
    // Token sesi multi-customer: secure storage dulu, fallback/migrasi
    // dari SharedPreferences lama (brief §7/§9).
    final tok = await SessionStore().readToken();
    final sp = await SharedPreferences.getInstance();
    if (!mounted) return;
    setState(() {
      _baseUrl = sp.getString(_kPrefUrl) ?? '';
      _token = tok ?? '';
      // M-A: URL tersimpan lama bisa melanggar kebijakan M3 (mis. HTTP
      // produksi dari konfigurasi versi sebelumnya) — jangan crash saat
      // boot; perlakukan sebagai belum terkonfigurasi.
      _api = widget.api ?? _buildApiSafe(_baseUrl, _token);
    });
    if (!(_api?.isConfigured ?? false)) {
      setState(() => _phase = _Phase.unconfigured);
      return;
    }
    await refreshAll();
  }

  /// M-A: konstruksi Mt5Api dengan gerbang kebijakan M3 (Mt5UrlPolicy).
  /// URL tersimpan yang kini ilegal TIDAK membuat halaman crash — halaman
  /// kembali ke status "belum terkonfigurasi" dan URL baru divalidasi
  /// lewat dialog ATUR SERVER.
  Mt5Api? _buildApiSafe(String base, String token) {
    try {
      return Mt5Api(baseUrl: base, token: token);
    } on Mt5ApiError {
      return null;
    }
  }

  /// M-A: validasi URL jalur simpan manual memakai kebijakan M3 yang SAMA
  /// dengan klien auth/data (Mt5UrlPolicy) — tidak ada jalur alternatif.
  /// Null = valid. String kosong diterima (berarti menghapus konfigurasi
  /// — perilaku lama dipertahankan).
  String? _urlPolicyError(String rawUrl) {
    final base = rawUrl.trim();
    if (base.isEmpty) return null;
    try {
      Mt5UrlPolicy.validatedBase(base);
      return null;
    } on Mt5ApiError catch (e) {
      return e.message;
    }
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
  Future<void> refreshAll() async {
    final api = _api;
    if (api == null || !api.isConfigured) return;
    final epoch = ++_epoch;
    final range = _range();
    if (range == null) {
      // FIX Temuan 5: rentang custom invalid TIDAK BOLEH membiarkan data
      // rentang sebelumnya tampil apa adanya — tampilkan panduan.
      _activeRange = null;
      if (!mounted) return;
      setState(() {
        _phase = _Phase.invalidRange;
        _error = null;
      });
      return;
    }
    _activeRange = range; // snapshot untuk _loadMore & label interval
    if (!mounted) return;
    setState(() {
      _phase = _Phase.loading;
      _error = null;
    });
    try {
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
      // FIX race condition: bila ada refreshAll lebih baru, buang hasil
      // ini — respons yang selesai terakhir BUKAN milik filter terakhir.
      if (!mounted || epoch != _epoch) return;
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
      if (!mounted || epoch != _epoch) return;
      setState(() {
        _phase = _Phase.error;
        _error = e.friendly;
      });
    } catch (_) {
      if (!mounted || epoch != _epoch) return;
      setState(() {
        _phase = _Phase.error;
        _error = 'Gagal memuat data jurnal.';
      });
    }
  }

  Future<void> _loadMore() async {
    final api = _api;
    if (api == null || _loadingMore || _page >= _pages) return;
    // FIX race: pakai SNAPSHOT rentang fetch terakhir — bukan _range()
    // yang dihitung ulang saat tombol ditekan (dahulu halaman-2 dari
    // rentang lama bisa menyusul setelah refresh rentang baru).
    final range = _activeRange;
    if (range == null) return;
    final epoch = _epoch;
    setState(() => _loadingMore = true);
    try {
      final tl = await api.trades(
        page: _page + 1,
        from: range.fromUtc,
        to: range.toUtc,
        toExclusive: true,
        symbol: _symbolSel == 'SEMUA' ? null : _symbolSel,
      );
      // Bila refresh baru dimulai di tengah jalan, append ini basi — buang.
      if (!mounted || epoch != _epoch) return;
      setState(() {
        _trades = [..._trades, ...tl.items];
        _page = tl.page;
        _pages = tl.pages;
      });
    } on Mt5ApiError catch (e) {
      if (mounted && epoch == _epoch) _toast(e.friendly, err: true);
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
      await refreshAll();
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
    // L2 (audit PR #4): snapshot isi awal field token manual. tokC hanya bisa
    // berubah setelah titik ini bila user benar-benar mengetik — satu-satunya
    // sinyal niat simpan token manual yang valid. Tanpa ini, blok persist di
    // bawah menimpa token hasil login dengan snapshot basi.
    final tokInitial = tokC.text.trim();
    final emailC = TextEditingController();
    final passC = TextEditingController();
    // M-A: pesan galat validasi URL (kebijakan M3) untuk tombol SIMPAN —
    // ditampilkan di dalam sheet supaya penolakan terlihat jelas.
    final sheetUrlError = ValueNotifier<String?>(null);
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
          child: SingleChildScrollView(
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
                  'Alamat backend Flask di VPS (HTTPS). Masuk pakai akun '
                  'customer — token sesi disimpan di secure storage.',
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
                _textField(ctx, 'Email customer', 'nama@email.com', emailC,
                    false),
                const SizedBox(height: 10),
                _textField(ctx, 'Password', '********', passC, true),
                const SizedBox(height: 12),
                GoldButton(
                  'MASUK',
                  icon: Icons.login,
                  onTap: () async {
                    try {
                      final base = urlC.text.trim();
                      final auth = Mt5AuthApi(baseUrl: base);
                      final tok = await auth.login(
                        emailC.text.trim(),
                        passC.text,
                      );
                      if (!ctx.mounted) return;
                      Navigator.pop(ctx, true);
                      if (!mounted) return;
                      setState(() {
                        _baseUrl = base;
                        _token = tok;
                        _api = widget.api ??
                            Mt5Api(baseUrl: base, token: tok);
                      });
                      await refreshAll();
                      return;
                    } on Mt5ApiError catch (e) {
                      _toast(e.friendly, err: true);
                    } catch (_) {
                      _toast('Login gagal — cek URL/email/password',
                          err: true);
                    }
                  },
                ),
                const SizedBox(height: 16),
                Text(
                  'TOKEN MANUAL (LANJUTAN) — token sesi dari server',
                  style: labelSm(ctx, color: p.dim),
                ),
                const SizedBox(height: 8),
                _textField(ctx, 'Token sesi', 'xxxxxxxx...', tokC, true),
                const SizedBox(height: 16),
                ValueListenableBuilder<String?>(
                  valueListenable: sheetUrlError,
                  builder: (ctx, err, _) => err == null
                      ? const SizedBox.shrink()
                      : Padding(
                          padding: const EdgeInsets.only(bottom: 12),
                          child: Row(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Icon(Icons.report, size: 15, color: p.redSolid),
                              const SizedBox(width: 6),
                              Expanded(
                                child: Text(
                                  err,
                                  style: txt(ctx, 12, color: p.redSolid),
                                ),
                              ),
                            ],
                          ),
                        ),
                ),
                GoldButton(
                  'SIMPAN',
                  icon: Icons.save_outlined,
                  // M-A: validasi M3 sebelum sheet ditutup — URL HTTP
                  // non-loopback DITOLAK di sini; sheet tetap terbuka
                  // dan pesan galat ditampilkan.
                  onTap: () {
                    final err = _urlPolicyError(urlC.text);
                    sheetUrlError.value = err;
                    if (err != null) return;
                    Navigator.pop(ctx, true);
                  },
                ),
              ],
            ),
          ),
        );
      },
    );
    if (ok != true) return;
    final newBase = urlC.text.trim();
    // M-A: gerbang persist — validasi M3 yang sama dengan klien auth/data.
    // Tidak ada jalur alternatif: URL ilegal TIDAK tersimpan sama sekali
    // (URL maupun token tidak disentuh) dan user melihat pesan galat.
    final urlErr = _urlPolicyError(newBase);
    if (urlErr != null) {
      _toast(urlErr, err: true);
      return;
    }
    final sp = await SharedPreferences.getInstance();
    await sp.setString(_kPrefUrl, newBase);
    // L2 fix: tulis token HANYA bila field manual diedit eksplisit. Sebelumnya
    // baris ini selalu menulis tokC.text (snapshot token LAMA/kosong dari saat
    // sheet DIBUKA) — menimpa token BARU yang baru saja disimpan
    // Mt5AuthApi.login (mt5_auth.dart:190):
    //  * login pertama, field kosong -> store berisi '' -> sesi hilang tiap
    //    restart aplikasi (readToken -> null -> unconfigured);
    //  * login ulang, field pre-fill -> token lama menimpa token baru;
    //    rotasi sesi tidak pernah terjadi, token baru jadi yatim di
    //    auth_tokens server sampai TTL.
    final manualTok = tokC.text.trim();
    final manualChanged = manualTok != tokInitial;
    if (manualChanged) {
      await SessionStore().saveToken(manualTok);
    }
    if (!mounted) return;
    setState(() {
      _baseUrl = newBase;
      // L2 fix: tanpa edit manual, biarkan _token dari jalur MASUK (baris
      // 367) — memori dan store harus selalu sepakat.
      if (manualChanged) _token = manualTok;
      _api = widget.api ?? Mt5Api(baseUrl: _baseUrl, token: _token);
    });
    await refreshAll();
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
      case _Phase.invalidRange:
        return RefreshIndicator(
          color: p.goldSolid,
          onRefresh: refreshAll,
          child: _invalidRangeView(p),
        );
      case _Phase.ready:
        return RefreshIndicator(
          color: p.goldSolid,
          onRefresh: refreshAll,
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

  /// FIX Temuan 5: rentang custom belum lengkap/invalid -> TAMPILKAN
  /// PANDUAN, bukan statistik lama (dahulu early return ke _Phase.ready
  /// dan data rentang sebelumnya dirender tanpa penanda — keluhan
  /// "statistik lama muncul sebagai hasil filter baru").
  Widget _invalidRangeView(Palette p) {
    return SingleChildScrollView(
      physics: const AlwaysScrollableScrollPhysics(),
      padding: const EdgeInsets.fromLTRB(16, 16, 16, 24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SectionHead(Icons.cloud_sync_outlined, 'Jurnal MT5 Otomatis'),
          const SizedBox(height: 14),
          _filterRow(p),
          const SizedBox(height: 14),
          ObsCard(
            children: [
              Row(
                children: [
                  Icon(Icons.info_outline, size: 17, color: p.warm),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      'RENTANG TANGGAL BELUM LENGKAP',
                      style: labelSm(context, color: p.muted),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 10),
              Text(
                'Pilih tanggal MULAI dan AKHIR untuk rentang custom. '
                'Tanggal akhir dihitung penuh sampai tengah malam '
                '(interval [mulai, akhir + 1 hari) di zona $_tz).\n\n'
                'Statistik sengaja tidak ditampilkan agar tidak tertukar '
                'dengan hasil rentang sebelumnya. Pilih preset lain di atas '
                'untuk kembali memuat data.',
                style: txt(context, 13, color: p.muted, height: 1.5),
              ),
            ],
          ),
        ],
      ),
    );
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
                      onTap: refreshAll,
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
                  // FIX Temuan 2: nilai penuh, simbol dinamis per akun
                  // (dahulu "Rp346rb USD" — Rp hardcode + suffix kode).
                  FittedBox(
                    fit: BoxFit.scaleDown,
                    alignment: Alignment.centerLeft,
                    child: Text(
                      fmtMoney(a?.balance ?? 0, _cur()),
                      style: mono(
                        context,
                        15,
                        w: FontWeight.w600,
                        color: p.ink,
                      ),
                    ),
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
                  FittedBox(
                    fit: BoxFit.scaleDown,
                    alignment: Alignment.centerLeft,
                    child: Text(
                      fmtMoney(a?.equity ?? 0, _cur()),
                      style: mono(
                        context,
                        15,
                        w: FontWeight.w600,
                        color: p.mint,
                      ),
                    ),
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
                  FittedBox(
                    fit: BoxFit.scaleDown,
                    alignment: Alignment.centerLeft,
                    child: Text(
                      '1:${a?.leverage ?? '-'}',
                      style: mono(
                        context,
                        15,
                        w: FontWeight.w600,
                        color: p.warm,
                      ),
                    ),
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
    final tzShort = _tz.id == 'UTC' ? 'UTC' : 'WIB';
    // FIX Temuan 4 + race: label interval = milik FETCH TERAKHIR
    // (_activeRange). Dahulu rangeFor() dipanggil ulang saat build dan
    // memakai DateTime.now(), sehingga teks bisa bergeser dari interval
    // yang benar-benar dipakai data yang tampil.
    final range = _phase == _Phase.ready ? _activeRange : _range();
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
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(Icons.date_range, size: 12, color: p.dim),
            const SizedBox(width: 5),
            Expanded(
              child: Text(
                // FIX Temuan 4: label boleh membungkus 2 baris — dahulu
                // maxLines:1 + ellipsis memotong ±65 karakter di layar 412dp.
                intervalText,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                softWrap: true,
                style: mono(context, 11.5, color: p.warm),
              ),
            ),
          ],
        ),
        const SizedBox(height: 8),
        Row(
          children: [
            BadgePill(
              // desimal koma konsisten (dahulu titik di sini, koma di grid)
              'winrate ${_stats.winRate != null ? one(_stats.winRate!) : '-'}%',
              color: p.gold,
              bg: p.goldsoft,
            ),
          ],
        ),
      ],
    );
  }

  /// Terapkan rentang custom dari pemilih tanggal (atau test).
  /// visibleForTesting: jalur satu-satunya men-set preset custom sehingga
  /// kasus "custom belum lengkap/invalid" bisa direproduksi di widget test.
  @visibleForTesting
  Future<void> applyCustomRange(DateTime? start, DateTime? end) async {
    setState(() {
      _customFrom = start;
      _customTo = end;
      _preset = DatePreset.custom;
    });
    await refreshAll();
  }

  Future<void> _pickPreset(DatePreset preset) async {
    if (preset != DatePreset.custom) {
      setState(() => _preset = preset);
      await refreshAll();
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
    await applyCustomRange(start, end);
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
    await refreshAll();
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
    await refreshAll();
  }

  Widget _cell(String label, String val, Color c) {
    final p = Palette.of(context);
    return Expanded(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(label, style: labelSm(context, color: p.dim)),
          const SizedBox(height: 4),
          // FIX Temuan 1: nilai tampil PENUH — skala turun bila ruang kurang
          // (FittedBox), TANPA ellipsis yang memotong digit (dahulu
          // maxLines:1 + ellipsis membuat +Rp22.036,79 terpotong di sel).
          FittedBox(
            fit: BoxFit.scaleDown,
            alignment: Alignment.centerLeft,
            child: Text(
              val,
              style: mono(context, 14, w: FontWeight.w600, color: c),
            ),
          ),
        ],
      ),
    );
  }

  Widget _statsGrid(Palette p) {
    final s = _stats;
    final cur = _cur();
    // FIX Temuan 6: nominal + persen DD berdampingan, basis dinyatakan
    // eksplisit (CLOSED P/L). Persen N/A bila backend mengirim null
    // (puncak P/L kumulatif <= 0 — penyebut tidak valid) — BUKAN 0%.
    final ddPct = _equity.maxDrawdownPct;
    final maxDdText = s.totalClosed == 0
        ? '-'
        : '${fmtMoney(-_equity.maxDrawdown, cur)}'
              ' (${ddPct != null ? '${one(ddPct)}%' : 'N/A'})';
    final cells = <Widget>[
      _cell('WINRATE', s.winRate != null ? '${one(s.winRate!)}%' : '-', p.gold),
      _cell(
        'NET P/L',
        fmtMoney(s.netProfit, cur, signed: true),
        s.netProfit >= 0 ? p.mint : p.red,
      ),
      _cell(
        'PROFIT FACTOR',
        s.profitFactor != null ? two(s.profitFactor) : 'N/A',
        s.profitFactor != null ? p.mint : p.dim,
      ),
      _cell('MENANG / KALAH', '${s.wins} / ${s.losses}', p.ink),
      _cell(
        'AVG WIN',
        s.averageWin != null ? fmtMoney(s.averageWin!, cur, signed: true) : '-',
        p.mint,
      ),
      _cell(
        'AVG LOSS',
        s.averageLoss != null ? fmtMoney(-s.averageLoss!, cur) : '-',
        p.red,
      ),
      _cell(
        'EXPECTANCY',
        s.expectancy != null ? fmtMoney(s.expectancy!, cur, signed: true) : '-',
        p.ink,
      ),
      _cell('VOLUME', '${fmtLots(s.totalVolume)} lot', p.warm),
      _cell('MAX DD · CLOSED P/L', maxDdText, p.red),
    ];
    return LayoutBuilder(
      builder: (context, cons) => ObsCard(
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
          // FIX Temuan 1 (responsif): 3 sel per baris hanya bila layar
          // cukup lebar; layar sempit memakai 2 kolom sehingga angka
          // panjang (−Rp1.564.837,89) punya ruang, bukan terpotong.
          for (var i = 0; i < cells.length; i += cons.maxWidth >= 480 ? 3 : 2)
            Padding(
              padding: const EdgeInsets.only(bottom: 10),
              child: Row(
                children: [
                  for (
                    var j = i;
                    j < i + (cons.maxWidth >= 480 ? 3 : 2) && j < cells.length;
                    j++
                  ) ...[if (j > i) const SizedBox(width: 12), cells[j]],
                ],
              ),
            ),
          // FIX Temuan 3: definisi volume eksplisit — mencegah salah paham
          // double counting (entry vs exit vs round-turn).
          Text(
            'Volume = total lot keluar (sisi close).',
            style: txt(context, 10.5, color: p.dim),
          ),
        ],
      ),
    );
  }

  Widget _equityCard(Palette p) {
    final pts = _equity.points;
    final ddPct = _equity.maxDrawdownPct;
    return ObsCard(
      children: [
        Row(
          children: [
            Icon(Icons.show_chart, size: 15, color: p.gold),
            const SizedBox(width: 7),
            Expanded(
              child: Text(
                'KURVA P/L TRADING (NET)',
                style: labelSm(context, color: p.muted),
              ),
            ),
            BadgePill(
              // FIX Temuan 6: pill menyatakan basis (CLOSED-P/L) dan
              // tampil N/A saat persen tidak punya penyebut valid —
              // dahulu "DD 0,0%" padahal akun belum pernah untung.
              ddPct != null
                  ? 'DD CLOSED-P/L ${one(ddPct)}%'
                  : 'DD CLOSED-P/L N/A',
              color: (ddPct ?? 0) > 0 ? p.red : p.dim,
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
          style: txt(context, 10.5, color: p.dim),
        ),
        const SizedBox(height: 4),
        Row(
          children: [
            // FIX Temuan 1: footer kurva dulu tanpa Flexible — RenderFlex
            // overflow saat angka panjang. Seluruh grup label+nilai kini
            // FittedBox: nilai PENUH, mengecil seragam bila ruang kurang.
            Flexible(
              child: FittedBox(
                fit: BoxFit.scaleDown,
                alignment: Alignment.centerLeft,
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(
                      'Net kumulatif: ',
                      style: txt(context, 11, color: p.dim),
                    ),
                    const SizedBox(width: 4),
                    Text(
                      fmtMoney(_equity.finalNetCum, _cur(), signed: true),
                      style: mono(
                        context,
                        12,
                        w: FontWeight.w600,
                        color: _equity.finalNetCum >= 0 ? p.mint : p.red,
                      ),
                    ),
                  ],
                ),
              ),
            ),
            const SizedBox(width: 12),
            Flexible(
              child: FittedBox(
                fit: BoxFit.scaleDown,
                alignment: Alignment.centerLeft,
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text('Deposit/WD: ', style: txt(context, 11, color: p.dim)),
                    const SizedBox(width: 4),
                    Text(
                      fmtMoney(_equity.balanceOpsCum, _cur()),
                      style: mono(
                        context,
                        12,
                        w: FontWeight.w600,
                        color: p.warm,
                      ),
                    ),
                  ],
                ),
              ),
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
                // FIX Temuan 1: kolom kiri dibatasi Expanded agar metadata
                // membungkus — nilai uang di kanan tetap utuh & utama.
                Expanded(
                  child: Column(
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
                          Flexible(
                            child: Text(
                              t.symbol,
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: mono(context, 13, w: FontWeight.w600),
                            ),
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
                        // FIX Temuan 3: lot — dahulu shortIdr → "Rp0" utk 0,02
                        '${fmtLots(t.volumeOut)}/${fmtLots(t.volumeIn)} lot'
                        '${t.pips != null ? ' · ${t.pips! >= 0 ? '+' : '−'}'
                                  '${one(t.pips!.abs())} pip' : ''}',
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
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
                                bg: t.exitReason == 'tp'
                                    ? p.goodsoft
                                    : p.redsoft,
                              ),
                            if (t.exitReason != null && t.rr != null)
                              const SizedBox(width: 6),
                            if (t.rr != null)
                              BadgePill(
                                'RR ${two(t.rr!)}',
                                color: p.warm,
                                bg: p.cardHi,
                              ),
                          ],
                        ),
                      ],
                    ],
                  ),
                ),
                // Nilai P/L utama: tidak pernah terpotong — menyusut bila
                // perlu (FittedBox) dalam batas lebar tetap.
                ConstrainedBox(
                  constraints: const BoxConstraints(maxWidth: 170),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.end,
                    children: [
                      FittedBox(
                        fit: BoxFit.scaleDown,
                        child: Text(
                          fmtMoney(t.netProfit, _cur(), signed: true),
                          style: mono(
                            context,
                            14,
                            w: FontWeight.w600,
                            color: pnlC,
                          ),
                        ),
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
                // FIX Temuan 3: lot, bukan rupiah
                '${fmtLots(d.trade.volumeIn)} / ${fmtLots(d.trade.volumeOut)} lot',
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
                          '${one(d.trade.pips!.abs())} '
                          '(jarak ${d.trade.pipDistance != null ? one(d.trade.pipDistance!) : '-'})'
                    : '-',
              ),
              _kv(
                ctx,
                p,
                'Profit',
                fmtMoney(d.trade.profit, _cur(), signed: true),
              ),
              _kv(ctx, p, 'Komisi', fmtMoney(d.trade.commission, _cur())),
              _kv(ctx, p, 'Swap', fmtMoney(d.trade.swap, _cur())),
              _kv(ctx, p, 'Fee', fmtMoney(d.trade.fee, _cur())),
              _kv(
                ctx,
                p,
                'NET P/L',
                fmtMoney(d.trade.netProfit, _cur(), signed: true),
                color: d.trade.netProfit >= 0 ? p.mint : p.red,
              ),
              // RR JUJUR: tanpa SL awal tampil N/A — tidak mengarang angka.
              _kv(
                ctx,
                p,
                'Risk:Reward',
                d.trade.rr != null ? '1 : ${two(d.trade.rr!)}' : 'N/A',
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
