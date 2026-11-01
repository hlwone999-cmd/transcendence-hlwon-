// Transcendence — mt5_api.dart
// Klien REST backend Jurnal MT5 (Flask di VPS). Autentikasi pakai header
// X-API-Token. Semua method melempar Mt5ApiError dengan `kind` yang jelas
// sehingga UI bisa menampilkan pesan ramah tanpa crash.
//
// http.Client diinjeksikan (default http.Client) supaya test bisa pakai
// MockClient — tidak ada panggilan jaringan nyata saat test.
import 'dart:convert';

import 'package:http/http.dart' as http;

import 'mt5_models.dart';

enum Mt5ApiErrorKind { network, auth, server, badResponse }

class Mt5ApiError implements Exception {
  final Mt5ApiErrorKind kind;
  final String message;
  Mt5ApiError(this.kind, this.message);

  @override
  String toString() => message;

  String get friendly {
    switch (kind) {
      case Mt5ApiErrorKind.network:
        return 'Tidak bisa menyambung ke server — cek URL/internet.';
      case Mt5ApiErrorKind.auth:
        return 'Token API ditolak (401) — periksa pengaturan server.';
      case Mt5ApiErrorKind.server:
        return message.isEmpty ? 'Server bermasalah (5xx).' : message;
      case Mt5ApiErrorKind.badResponse:
        return message.isEmpty ? 'Balasan server tidak dikenal.' : message;
    }
  }
}

class Mt5Api {
  /// Contoh baseUrl: https://jurnal.domainku.com  (tanpa /api)
  final String baseUrl;
  final String token;
  final http.Client _http;

  Mt5Api({required String baseUrl, required this.token, http.Client? client})
      : baseUrl = baseUrl.trim().replaceAll(RegExp(r'/+$'), ''),
        _http = client ?? http.Client();

  bool get isConfigured => baseUrl.isNotEmpty && token.isNotEmpty;

  Uri _uri(String path, [Map<String, String>? query]) {
    final base = baseUrl.startsWith('http')
        ? baseUrl
        : 'https://$baseUrl'; // default HTTPS — API di VPS harus di belakang TLS
    return Uri.parse('$base/api/v1$path')
        .replace(queryParameters: query == null || query.isEmpty ? null : query);
  }

  Map<String, String> get _headers =>
      {'X-API-Token': token, 'Content-Type': 'application/json'};

  Future<dynamic> _send(Future<http.Response> Function() fn) async {
    http.Response r;
    try {
      r = await fn().timeout(const Duration(seconds: 25));
    } catch (_) {
      throw Mt5ApiError(Mt5ApiErrorKind.network, '');
    }
    if (r.statusCode == 401) {
      throw Mt5ApiError(Mt5ApiErrorKind.auth, '');
    }
    if (r.statusCode >= 500) {
      throw Mt5ApiError(Mt5ApiErrorKind.server,
          'Server error ${r.statusCode}');
    }
    dynamic j;
    try {
      j = jsonDecode(utf8.decode(r.bodyBytes));
    } catch (_) {
      throw Mt5ApiError(Mt5ApiErrorKind.badResponse,
          'Balasan bukan JSON (status ${r.statusCode})');
    }
    if (r.statusCode >= 400) {
      String msg = 'HTTP ${r.statusCode}';
      if (j is Map && j['error'] is Map && j['error']['message'] is String) {
        msg = j['error']['message'] as String;
      }
      throw Mt5ApiError(Mt5ApiErrorKind.badResponse, msg);
    }
    return j;
  }

  Future<dynamic> _get(String path, [Map<String, String>? q]) =>
      _send(() => _http.get(_uri(path, q), headers: _headers));

  Future<dynamic> _post(String path, {Object? body}) => _send(
      () => _http.post(_uri(path),
          headers: _headers, body: jsonEncode(body ?? {})));

  // ------------------------------------------------------------ endpoint
  Future<Mt5Status> status() async =>
      Mt5Status.fromJson(await _get('/mt5/status') as Map<String, dynamic>);

  Future<Mt5Account> account() async =>
      Mt5Account.fromJson(await _get('/mt5/account') as Map<String, dynamic>);

  /// Trigger sinkronisasi. [full] = seluruh riwayat.
  Future<Map<String, dynamic>> triggerSync({bool full = false}) async =>
      (await _post('/mt5/sync', body: {'mode': full ? 'full' : 'incremental'}))
          as Map<String, dynamic>;

  Future<Map<String, dynamic>> syncStatus() async =>
      (await _get('/mt5/sync/status')) as Map<String, dynamic>;

  Future<Mt5TradeList> trades({
    int page = 1,
    int pageSize = 50,
    String? from,
    String? to,
    String? symbol,
    String status = 'all',
  }) async {
    final q = <String, String>{
      'page': '$page',
      'page_size': '$pageSize',
      'status': status,
    };
    if (from != null && from.isNotEmpty) q['from'] = from;
    if (to != null && to.isNotEmpty) q['to'] = to;
    if (symbol != null && symbol.isNotEmpty && symbol != 'SEMUA') {
      q['symbol'] = symbol;
    }
    return Mt5TradeList.fromJson(
        await _get('/journal/trades', q) as Map<String, dynamic>);
  }

  Future<Mt5TradeDetail> tradeDetail(int positionId) async =>
      Mt5TradeDetail.fromJson(
          await _get('/journal/trades/$positionId') as Map<String, dynamic>);

  Future<Mt5Stats> stats({String? from, String? to, String? symbol}) async {
    final q = <String, String>{};
    if (from != null && from.isNotEmpty) q['from'] = from;
    if (to != null && to.isNotEmpty) q['to'] = to;
    if (symbol != null && symbol.isNotEmpty && symbol != 'SEMUA') {
      q['symbol'] = symbol;
    }
    return Mt5Stats.fromJson(
        await _get('/journal/stats', q) as Map<String, dynamic>);
  }

  Future<Mt5Equity> equityCurve({String? from, String? to, String? symbol}) async {
    final q = <String, String>{};
    if (from != null && from.isNotEmpty) q['from'] = from;
    if (to != null && to.isNotEmpty) q['to'] = to;
    if (symbol != null && symbol.isNotEmpty && symbol != 'SEMUA') {
      q['symbol'] = symbol;
    }
    return Mt5Equity.fromJson(
        await _get('/journal/equity-curve', q) as Map<String, dynamic>);
  }

  Future<List<String>> symbols() async {
    final j = await _get('/symbols') as Map<String, dynamic>;
    return (j['items'] as List<dynamic>? ?? [])
        .map((e) => (e as Map<String, dynamic>)['symbol'] as String? ?? '')
        .where((s) => s.isNotEmpty)
        .toList();
  }
}
