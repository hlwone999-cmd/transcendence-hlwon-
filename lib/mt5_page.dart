// Transcendence — mt5_page.dart
// Tab "Jurnal MT5": jurnal trading OTOMATIS dari akun MT5 lewat backend
// Flask (mt5linux) di VPS. Backend read-only — halaman ini tidak pernah
// mengirim order.
//
// REDESIGN UI/UX (keputusan D1-D4):
//  - HANYA 3 input: nomor akun MT5, server broker (dropdown Exness +
//    Lainnya manual), password investor (tombol lihat/sembunyi).
//  - TIDAK ADA input URL server / email customer / password customer /
//    token manual — URL backend ditentukan konfigurasi aplikasi
//    (AppConfig, --dart-define TRANSCENDENCE_API_URL).
//  - HUBUNGKAN AKUN MT5 -> POST /mt5/connect (self-link otomatis).
//  - Status koneksi selalu tampil; PUTUSKAN KONEKSI -> POST
//    /mt5/disconnect yang HANYA mencabut token sesi — riwayat jurnal
//    tidak dihapus (D3); unbind tetap hanya via API/admin.
//  - Password investor hanya hidup di memori (controller) selama proses
//    koneksi dan dibersihkan setelahnya; tidak pernah disimpan lokal.
//
// Gaya UI: Obsidian Precision (ui_kit.dart) — konsisten dengan tab lain.
// Fitur halaman ini TIDAK menyentuh kalkulator/engine sama sekali.
import 'package:flutter/material.dart';

import 'app_config.dart';
import 'engine.dart' show fmtLots, fmtMoney, one, two;
import 'mt5_api.dart';
import 'mt5_auth.dart';
import 'mt5_filters.dart';
import 'mt5_models.dart';
import 'mt5_servers.dart';
import 'ui_kit.dart';

class Mt5JournalPage extends StatefulWidget {
  final Mt5Api? api; // injeksi untuk test (data)
  final Mt5AuthApi? authApi; // injeksi untuk test (auth/connect)
  const Mt5JournalPage({super.key, this.api, this.authApi});

  @override
  State<Mt5JournalPage> createState() => _Mt5JournalPageState();
}

enum _Phase { boot, connectForm, loading, ready, invalidRange, error }

/// Status mesin koneksi (hardening post-redesign, requirement 3):
/// - none      : netral (belum/telah selesai)
/// - verifying : MEMVERIFIKASI — POST /mt5/connect berjalan, server
///               sedang memverifikasi kredensial + login nyata ke broker
/// - preparing : MENYIAPKAN TERMINAL — sesi diterima, menunggu /mt5/status
///               membuktikan koneksi live (sumber kebenaran TERHUBUNG)
/// - failed    : GAGAL — verifikasi live tidak berhasil dalam batas waktu
///               (sesi tetap ada; riwayat jurnal tetap bisa dibuka)
enum _ConnectStage { none, verifying, preparing, failed }

class _Mt5JournalPageState extends State<Mt5JournalPage> {
  _Phase _phase = _Phase.boot;
  String? _error;

  Mt5Api? _api;
  String _baseUrl = '';
  String _token = '';

  // ---- form koneksi (D1): tiga input saja ------------------------------
  final TextEditingController _loginC = TextEditingController();
  final TextEditingController _serverManualC = TextEditingController();
  final TextEditingController _passC = TextEditingController();
  String _serverSel = kExnessServers.first.value;
  bool _obscurePass = true;
  _ConnectStage _stage = _ConnectStage.none;
  String? _formError;
  String? _connectNote; // alasan GAGAL verifikasi live (tanpa kredensial)
  bool _disconnecting = false;

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

  @override
  void dispose() {
    _loginC.dispose();
    _serverManualC.dispose();
    // password investor tidak boleh tertinggal di memori lebih lama dari
    // halaman itu sendiri (syarat implementasi #2)
    _passC.dispose();
    super.dispose();
  }

  Future<void> _boot() async {
    // D1: URL backend dari KONFIGURASI APLIKASI — bukan input user.
    _baseUrl = AppConfig.apiBaseUrl.trim();
    // Token sesi multi-customer: secure storage (fallback/migrasi dari
    // SharedPreferences lama — brief §7/§9).
    final tok = await SessionStore().readToken();
    if (!mounted) return;
    setState(() {
      _token = tok ?? '';
      // M-A/M3: URL konfigurasi yang ilegal (mis. HTTP produksi dari
      // build lama) tidak membuat halaman crash — tampilkan form koneksi.
      _api = widget.api ?? _buildApiSafe(_baseUrl, _token);
    });
    // D1 + M2: TANPA SESI -> selalu FORM KONEKSI, apa pun kondisi URL —
    // semua endpoint jurnal butuh token sesi, jadi refresh tanpa sesi
    // hanya menghasilkan 401 yang mengembalikan halaman ke form lagi.
    // (Perbaikan boot: kini form langsung, tanpa round-trip 401.)
    if (_token.isEmpty ||
        _api == null ||
        !(_api?.isConfigured ?? false)) {
      setState(() => _phase = _Phase.connectForm);
      return;
    }
    await refreshAll();
  }

  /// M-A: konstruksi Mt5Api dengan gerbang kebijakan M3 (Mt5UrlPolicy).
  /// URL yang kini ilegal TIDAK membuat halaman crash — halaman kembali
  /// ke form koneksi.
  Mt5Api? _buildApiSafe(String base, String token) {
    try {
      return Mt5Api(baseUrl: base, token: token);
    } on Mt5ApiError {
      return null;
    }
  }

  /// Klien auth/connect memakai URL konfigurasi yang sama (M3 satu sumber).
  Mt5AuthApi _authApi() =>
      widget.authApi ?? Mt5AuthApi(baseUrl: _baseUrl);

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
      if (e.kind == Mt5ApiErrorKind.auth) {
        // M2 + redesign: token ditolak server (kedaluwarsa/dicabut oleh
        // PUTUSKAN KONEKSI di perangkat lain) -> sesi lokal dibersihkan
        // dan halaman kembali ke FORM KONEKSI, bukan layar error.
        await SessionStore().clear();
        if (!mounted) return;
        setState(() {
          _token = '';
          _api = widget.api ?? _buildApiSafe(_baseUrl, '');
          _phase = _Phase.connectForm;
          _error = null;
        });
        return;
      }
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

  // -------------------------------------------------- koneksi (D1/D3)
  String _selectedServer() => _serverSel == kServerLainnya
      ? _serverManualC.text.trim()
      : _serverSel;

  /// Validasi ringan form koneksi. Return pesan galat atau null.
  String? _validateForm() {
    final login = int.tryParse(_loginC.text.trim());
    if (login == null || login <= 0) {
      return 'Nomor akun MT5 wajib angka yang valid';
    }
    final serverErr = validateManualServer(_selectedServer());
    if (serverErr != null) return serverErr;
    final pass = _passC.text;
    if (pass.isEmpty) return 'Password investor wajib diisi';
    if (pass.length > 256) return 'Password terlalu panjang (maks 256)';
    return null;
  }

  /// HUBUNGKAN AKUN MT5 (D1): self-link otomatis via POST /mt5/connect.
  /// Password investor hanya hidup di memori selama proses ini —
  /// controller dibersihkan di SEMUA jalur keluar (sukses/gagal).
  ///
  /// Status (requirement 1/3): MEMVERIFIKASI selama request berjalan;
  /// setelah sesi diterima status menjadi MENYIAPKAN TERMINAL sampai
  /// /mt5/status (probe live backend) membuktikan TERHUBUNG. TERHUBUNG
  /// tidak pernah dinyatakan hanya karena POST sukses.
  Future<void> _connectAccount() async {
    if (_stage == _ConnectStage.verifying) return;
    final err = _validateForm();
    if (err != null) {
      setState(() => _formError = err);
      return;
    }
    if (_baseUrl.isEmpty) {
      setState(() {
        _formError = 'URL backend belum dikonfigurasi pada build aplikasi '
            '(--dart-define TRANSCENDENCE_API_URL).';
      });
      return;
    }
    final login = int.parse(_loginC.text.trim());
    final server = _selectedServer();
    final pass = _passC.text; // hanya di memori selama proses koneksi
    setState(() {
      _stage = _ConnectStage.verifying; // MEMVERIFIKASI…
      _formError = null;
    });
    try {
      final res = await _authApi().connectAccount(
        login: login,
        server: server,
        password: pass,
      );
      _passC.clear(); // password keluar dari memori segera
      final tok = res['token'] as String;
      if (!mounted) return;
      setState(() {
        _token = tok;
        _api = widget.api ?? _buildApiSafe(_baseUrl, tok);
        _stage = _ConnectStage.preparing; // MENYIAPKAN TERMINAL…
      });
      await refreshAll();
      if (!mounted) return;
      if (_status?.connected == true) {
        // probe live backend membuktikan koneksi — baru boleh TERHUBUNG
        setState(() => _stage = _ConnectStage.none);
      } else {
        // terminal belum terbukti live (mis. backend melaporkan
        // terminal_pending) — tunggu konfirmasi /mt5/status
        await _awaitTerminalConfirmation();
      }
    } on Mt5ApiError catch (e) {
      _passC.clear();
      if (!mounted) return;
      setState(() {
        _stage = _ConnectStage.none; // GAGAL tampil via _formError
        _formError = e.friendly;
      });
    } catch (_) {
      _passC.clear();
      if (!mounted) return;
      setState(() {
        _stage = _ConnectStage.none;
        _formError =
            'Gagal menghubungkan — periksa data akun dan koneksi internet.';
      });
    }
  }

  /// MENYIAPKAN TERMINAL -> TERHUBUNG / GAGAL: poll /mt5/status (probe
  /// live backend) sampai terbukti terhubung atau batas siklus habis.
  /// Sumber kebenaran status adalah probe NYATA backend, bukan asumsi.
  Future<void> _awaitTerminalConfirmation() async {
    final api = _api;
    if (api == null) return;
    for (var i = 0; i < 4; i++) {
      await Future<void>.delayed(const Duration(seconds: 2));
      if (!mounted) return;
      try {
        final st = await api.status();
        if (!mounted) return;
        if (st.connected) {
          setState(() {
            _status = st;
            _stage = _ConnectStage.none; // TERHUBUNG terbukti
          });
          _toast('TERHUBUNG — kredensial terverifikasi terminal MT5.');
          return;
        }
        setState(() => _status = st);
      } on Mt5ApiError {
        // probe gagal (jaringan) — ulangi pada siklus berikutnya
      }
    }
    if (!mounted) return;
    setState(() {
      _stage = _ConnectStage.failed; // GAGAL — sesi tetap, riwayat terbuka
      _connectNote = _status?.reason ??
          'Terminal MT5 belum terverifikasi ke server broker dalam batas '
              'waktu. Riwayat jurnal tetap bisa dibuka — coba verifikasi '
              'ulang atau hubungi admin.';
    });
  }

  /// COBA VERIFIKASI ULANG (banner GAGAL): probe /mt5/status sekali —
  /// probe live backend mencoba login nyata ke broker.
  Future<void> _reverify() async {
    if (_stage == _ConnectStage.preparing) return;
    setState(() {
      _stage = _ConnectStage.preparing;
      _connectNote = null;
    });
    try {
      final st = await _api?.status();
      if (!mounted) return;
      if (st != null && st.connected) {
        setState(() {
          _status = st;
          _stage = _ConnectStage.none;
        });
        _toast('TERHUBUNG — kredensial terverifikasi terminal MT5.');
      } else {
        if (st != null) _status = st;
        setState(() {
          _stage = _ConnectStage.failed;
          _connectNote = _status?.reason ??
              'Terminal MT5 belum terverifikasi ke server broker.';
        });
      }
    } on Mt5ApiError catch (e) {
      if (!mounted) return;
      setState(() {
        _stage = _ConnectStage.failed;
        _connectNote = e.friendly;
      });
    }
  }

  /// PUTUSKAN KONEKSI (D3): konfirmasi eksplisit sebelum logout.
  Future<void> _confirmDisconnect() async {
    final p = Palette.of(context);
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: p.surface1,
        title: Text('PUTUSKAN KONEKSI?',
            style: txt(ctx, 15, w: FontWeight.w600)),
        content: Text(
          'Sesi akan ditutup dan token dicabut. Riwayat jurnal TETAP '
          'tersimpan dan tersedia lagi setelah terhubung kembali.',
          style: txt(ctx, 13, color: p.muted, height: 1.5),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: Text('BATAL',
                style: mono(ctx, 12, color: p.muted, w: FontWeight.w600)),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: Text('PUTUSKAN',
                style: mono(ctx, 12, color: p.red, w: FontWeight.w600)),
          ),
        ],
      ),
    );
    if (ok != true) return;
    await _disconnectNow();
  }

  /// Eksekusi PUTUSKAN KONEKSI (D3): POST /mt5/disconnect HANYA mencabut
  /// token sesi. Tidak ada pemanggilan unbind — riwayat jurnal, akun
  /// ter-bind, dan kredensial tidak disentuh.
  Future<void> _disconnectNow() async {
    if (_disconnecting) return;
    final tok = _token;
    setState(() => _disconnecting = true);
    bool serverRevoked = false;
    try {
      if (tok.isNotEmpty) {
        await _authApi().disconnect(tok);
        serverRevoked = true;
      }
    } on Mt5ApiError {
      // revoke server gagal (mis. offline) — token lokal tetap dibuang;
      // token basi akan ditolak server saat dipakai (M2) dan halaman
      // kembali ke form koneksi.
    } finally {
      if (mounted) {
        setState(() {
          _disconnecting = false;
          _token = '';
          _api = widget.api ?? _buildApiSafe(_baseUrl, '');
          _status = null;
          _account = null;
          _trades = [];
          _stage = _ConnectStage.none;
          _connectNote = null;
          _phase = _Phase.connectForm;
        });
        _toast(serverRevoked
            ? 'Koneksi diputus — riwayat jurnal tetap tersimpan.'
            : 'Sesi lokal dibersihkan.');
      }
    }
  }

  // ------------------------------------------------------------ form UI
  Widget _serverDropdown(Palette p) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('SERVER BROKER', style: labelSm(context, color: p.dim)),
        const SizedBox(height: 6),
        Container(
          decoration: BoxDecoration(
            color: p.surface1,
            borderRadius: BorderRadius.circular(14),
            border: Border.all(width: 1, color: p.line),
          ),
          padding: const EdgeInsets.symmetric(horizontal: 12),
          child: DropdownButtonHideUnderline(
            child: DropdownButton<String>(
              value: _serverSel,
              isExpanded: true,
              icon: Icon(Icons.expand_more, size: 18, color: p.dim),
              dropdownColor: p.surface1,
              items: kServerOptions
                  .map(
                    (o) => DropdownMenuItem<String>(
                      value: o.value,
                      child: Text(
                        o.label,
                        style: mono(context, 14, w: FontWeight.w600),
                      ),
                    ),
                  )
                  .toList(),
              onChanged: (v) {
                if (v == null || v == _serverSel) return;
                setState(() {
                  _serverSel = v;
                  _formError = null;
                });
              },
            ),
          ),
        ),
        const SizedBox(height: 6),
        Text(
          'Daftar server Exness di atas tidak lengkap dan bisa berbeda '
          'per akun — pilih "Lainnya (isi manual)" bila server Anda tidak '
          'ada di daftar.',
          style: txt(context, 10.5, color: p.dim),
        ),
      ],
    );
  }

  /// Password investor dengan tombol lihat/sembunyi (D1).
  Widget _passwordField(Palette p) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('PASSWORD INVESTOR', style: labelSm(context, color: p.dim)),
        const SizedBox(height: 6),
        Container(
          decoration: BoxDecoration(
            color: p.surface1,
            borderRadius: BorderRadius.circular(14),
            border: Border.all(width: 1, color: p.line),
          ),
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
          child: TextField(
            controller: _passC,
            obscureText: _obscurePass,
            autocorrect: false,
            enableSuggestions: false,
            style: mono(context, 14, w: FontWeight.w600),
            decoration: InputDecoration(
              isDense: true,
              border: InputBorder.none,
              hintText: 'password investor (bukan password master)',
              hintStyle: mono(context, 13, color: p.dim),
              suffixIcon: IconButton(
                padding: EdgeInsets.zero,
                constraints: const BoxConstraints(minWidth: 32, minHeight: 32),
                icon: Icon(
                  _obscurePass
                      ? Icons.visibility_outlined
                      : Icons.visibility_off_outlined,
                  size: 18,
                  color: p.dim,
                ),
                onPressed: () => setState(() => _obscurePass = !_obscurePass),
              ),
            ),
            cursorColor: p.goldSolid,
            onSubmitted: (_) => _connectAccount(),
          ),
        ),
      ],
    );
  }

  /// Layar/form koneksi — pengganti layar "BELUM TERHUBUNG" + dialog
  /// ATUR SERVER lama. Hanya tiga input (D1); tidak ada URL/email/
  /// password customer/token manual.
  Widget _connectForm(Palette p) {
    final backendMissing = !AppConfig.hasApiBaseUrl;
    return SingleChildScrollView(
      padding: const EdgeInsets.fromLTRB(16, 6, 16, 24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SectionHead(Icons.cloud_sync_outlined, 'Jurnal MT5 Otomatis'),
          const SizedBox(height: 4),
          Text(
            'Hubungkan akun MT5 untuk memuat riwayat trade otomatis dari '
            'backend — read-only, tanpa eksekusi order.',
            style: txt(context, 12, color: p.muted),
          ),
          const SizedBox(height: 14),
          ObsCard(
            children: [
              Row(
                children: [
                  Icon(Icons.link, size: 17, color: p.gold),
                  const SizedBox(width: 8),
                  Text(
                    'HUBUNGKAN AKUN MT5',
                    style: labelSm(context, color: p.muted),
                  ),
                ],
              ),
              const SizedBox(height: 14),
              _textField(
                context,
                'Nomor akun MT5',
                '12345678',
                _loginC,
                false,
                keyboardType: TextInputType.number,
              ),
              const SizedBox(height: 10),
              _serverDropdown(p),
              if (_serverSel == kServerLainnya) ...[
                const SizedBox(height: 10),
                _textField(
                  context,
                  'Nama server (manual)',
                  'mis. Exness-MT5Real5',
                  _serverManualC,
                  false,
                ),
              ],
              const SizedBox(height: 10),
              _passwordField(p),
              const SizedBox(height: 6),
              Text(
                'Gunakan password INVESTOR (read-only) — backend hanya '
                'membaca riwayat trade, tidak bisa mengirim/mengubah/'
                'menutup order. Password hanya dipakai saat proses '
                'koneksi ini dan tidak pernah disimpan di perangkat.',
                style: txt(context, 11, color: p.dim, height: 1.5),
              ),
              if (backendMissing) ...[
                const SizedBox(height: 12),
                Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Icon(Icons.report, size: 15, color: p.warm),
                    const SizedBox(width: 6),
                    Expanded(
                      child: Text(
                        'URL backend belum dikonfigurasi pada build ini — '
                        'atur lewat --dart-define '
                        'TRANSCENDENCE_API_URL saat membangun aplikasi.',
                        style: txt(context, 11.5, color: p.warm, height: 1.4),
                      ),
                    ),
                  ],
                ),
              ],
              if (_formError != null) ...[
                const SizedBox(height: 12),
                Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Icon(Icons.error_outline, size: 15, color: p.red),
                    const SizedBox(width: 6),
                    Expanded(
                      child: Text(
                        _formError!,
                        style: txt(context, 12.5, color: p.red, height: 1.4),
                      ),
                    ),
                  ],
                ),
              ],
              const SizedBox(height: 14),
              SizedBox(
                width: double.infinity,
                child: GoldButton(
                  _stage == _ConnectStage.verifying
                      ? 'MEMVERIFIKASI…'
                      : 'HUBUNGKAN AKUN MT5',
                  icon: Icons.link,
                  onTap: _stage == _ConnectStage.verifying
                      ? null
                      : _connectAccount,
                ),
              ),
              if (_stage == _ConnectStage.verifying) ...[
                const SizedBox(height: 10),
                Row(
                  children: [
                    const SizedBox(
                      width: 14,
                      height: 14,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        'MEMVERIFIKASI — backend sedang memverifikasi '
                        'kredensial dan mencoba login read-only ke '
                        'server broker…',
                        style: txt(context, 11.5, color: p.muted),
                      ),
                    ),
                  ],
                ),
              ],
            ],
          ),
        ],
      ),
    );
  }

  Widget _textField(
    BuildContext ctx,
    String label,
    String hint,
    TextEditingController c,
    bool obscure, {
    TextInputType? keyboardType,
  }) {
    final p = Palette.of(ctx);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(label.toUpperCase(), style: labelSm(ctx, color: p.dim)),
        const SizedBox(height: 6),
        Container(
          decoration: BoxDecoration(
            color: p.surface1,
            borderRadius: BorderRadius.circular(14),
            border: Border.all(width: 1, color: p.line),
          ),
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
          child: TextField(
            controller: c,
            obscureText: obscure,
            autocorrect: false,
            enableSuggestions: false,
            keyboardType: keyboardType,
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
        return const SingleChildScrollView(
          padding: EdgeInsets.fromLTRB(16, 6, 16, 24),
          child: StateView(
            icon: Icons.cloud_sync_outlined,
            title: 'Memuat Jurnal MT5',
            message: 'Mengambil status akun & riwayat trade (read-only)…',
            loading: true,
          ),
        );
      case _Phase.connectForm:
        return _connectForm(p);
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
            padding: const EdgeInsets.fromLTRB(16, 6, 16, 24),
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
                _verificationBanner(p),
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
                  const StateView(
                    icon: Icons.inbox_outlined,
                    title: 'Belum ada trade pada rentang ini.',
                    message: 'Coba ubah rentang tanggal di filter atas.',
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
      padding: const EdgeInsets.fromLTRB(16, 6, 16, 24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SectionHead(Icons.cloud_sync_outlined, 'Jurnal MT5 Otomatis'),
          const SizedBox(height: 14),
          _filterRow(p),
          const SizedBox(height: 14),
          StateView(
            icon: Icons.date_range_outlined,
            title: 'RENTANG TANGGAL BELUM LENGKAP',
            message:
                ('Pilih tanggal MULAI dan AKHIR untuk rentang custom. '
                'Tanggal akhir dihitung penuh sampai tengah malam '
                '(interval [mulai, akhir + 1 hari) di zona $_tz).\n\n'
                'Statistik sengaja tidak ditampilkan agar tidak tertukar '
                'dengan hasil rentang sebelumnya. Pilih preset lain di atas '
                'untuk kembali memuat data.'),
          ),
        ],
      ),
    );
  }

  Widget _errorView(Palette p) {
    return SingleChildScrollView(
      padding: const EdgeInsets.fromLTRB(16, 6, 16, 24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SectionHead(Icons.cloud_sync_outlined, 'Jurnal MT5 Otomatis'),
          const SizedBox(height: 14),
          StateView(
            icon: Icons.cloud_off,
            color: p.red,
            title: 'GAGAL TERHUBUNG',
            message: _error ?? '-',
            action: Row(
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
                    'PUTUSKAN KONEKSI',
                    icon: Icons.link_off,
                    expanded: true,
                    onTap: _disconnecting ? null : _confirmDisconnect,
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  // -------------------------------------------------------------- kartu
  /// Banner GAGAL verifikasi live (requirement 1/3/4): sesi tetap ada dan
  /// riwayat jurnal tetap bisa dibuka, tapi status TIDAK BOLEH TERHUBUNG
  /// sampai probe live backend membuktikan. Tampilkan alasan (tanpa
  /// kredensial) + tombol verifikasi ulang.
  Widget _verificationBanner(Palette p) {
    if (_stage != _ConnectStage.failed) return const SizedBox.shrink();
    return ObsCard(
      children: [
        Row(
          children: [
            Icon(Icons.report, size: 17, color: p.red),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                'GAGAL — VERIFIKASI BROKER BELUM BERHASIL',
                style: txt(context, 13, w: FontWeight.w600, color: p.red),
              ),
            ),
          ],
        ),
        const SizedBox(height: 8),
        Text(
          _connectNote ?? 'Terminal MT5 belum terverifikasi.',
          style: txt(context, 12, color: p.muted, height: 1.5),
        ),
        const SizedBox(height: 10),
        GhostButton(
          _stage == _ConnectStage.preparing
              ? 'MEMERIKSA…'
              : 'COBA VERIFIKASI ULANG',
          icon: Icons.sync,
          onTap: _stage == _ConnectStage.preparing ? null : _reverify,
        ),
      ],
    );
  }

  Widget _statusCard(Palette p) {
    final st = _status;
    final conn = st?.connected ?? false;
    // Hardening requirement 3: empat status terpisah dan jelas —
    // TERHUBUNG hanya saat probe live backend sukses (st.connected),
    // MENYIAPKAN TERMINAL saat sesi menunggu bukti live, TERPUTUS bila
    // probe gagal di luar alur koneksi, GAGAL via banner verifikasi.
    final preparing = !conn && _stage == _ConnectStage.preparing;
    final last = st?.lastSync?['last_sync_finished_at'] as String?;
    final reason = st?.reason;
    final label = conn
        ? 'TERHUBUNG'
        : preparing
        ? 'MENYIAPKAN TERMINAL'
        : 'TERPUTUS';
    final stateColor = conn ? p.mint : (preparing ? p.warm : p.red);
    return ObsCard(
      children: [
        Row(
          children: [
            PulseDot(stateColor, size: 8),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                label,
                style: txt(context, 14, w: FontWeight.w600, color: stateColor),
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
              : (st?.terminalStatus == 'provisioning'
                    ? 'Terminal MT5 sedang disiapkan di server '
                          '(provisioning) — status akan diperbarui otomatis.'
                    : (reason ??
                          (preparing
                              ? 'Menunggu verifikasi live ke server broker…'
                              : 'Status belum diketahui.'))),
          style: txt(context, 12, color: p.muted),
        ),
        const SizedBox(height: 10),
        Row(
          children: [
            Expanded(
              child: GhostButton(
                'SINKRON PENUH',
                icon: Icons.history,
                onTap: _syncing ? null : () => _syncNow(full: true),
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: GhostButton(
                _disconnecting ? 'MEMUTUSKAN…' : 'PUTUSKAN KONEKSI',
                icon: Icons.link_off,
                onTap: _disconnecting ? null : _confirmDisconnect,
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
        borderRadius: BorderRadius.circular(16),
        child: InkWell(
          borderRadius: BorderRadius.circular(16),
          onTap: () => _openDetail(t),
          child: Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(16),
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
                    borderRadius: BorderRadius.circular(14),
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
