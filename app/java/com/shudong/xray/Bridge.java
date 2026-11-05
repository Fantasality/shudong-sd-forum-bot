package com.shudong.xray;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.util.Base64;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;

/** JS ↔ 原生桥：账号存储、GraphQL 代理、token 自动续期。 */
public class Bridge {
    private final MainActivity act;
    private final WebView web;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final Object ioLock = new Object();

    Bridge(MainActivity act, WebView web) {
        this.act = act;
        this.web = web;
    }

    // ---------- 存储 ----------

    private String pref(String key, String def) {
        return act.getSharedPreferences("sz", Context.MODE_PRIVATE).getString(key, def);
    }

    private void put(String key, String val) {
        act.getSharedPreferences("sz", Context.MODE_PRIVATE).edit().putString(key, val).apply();
    }

    private JSONObject accounts() {
        try {
            return new JSONObject(pref("accounts", "{}"));
        } catch (Exception e) {
            return new JSONObject();
        }
    }

    private void saveAccounts(JSONObject obj) {
        put("accounts", obj.toString());
    }

    // ---------- 对外接口 ----------

    @JavascriptInterface
    public String boot() {
        JSONObject out = new JSONObject();
        try {
            out.put("accounts", new JSONObject(pref("accounts", "{}")));
            out.put("cfg", new JSONObject(pref("cfg", "{}")));
            out.put("roster", new JSONObject(pref("roster", "{}")));
            out.put("version", "1.0.0");
            out.put("platform", "android");
        } catch (Exception ignored) {
        }
        return out.toString();
    }

    @JavascriptInterface
    public void save(String key, String value) {
        put(key, value);
    }

    @JavascriptInterface
    public void clear(String key) {
        act.getSharedPreferences("sz", Context.MODE_PRIVATE).edit().remove(key).apply();
    }

    @JavascriptInterface
    public String asset(String name) {
        try (InputStream is = act.getAssets().open(name)) {
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = is.read(buf)) > 0) bos.write(buf, 0, n);
            return new String(bos.toByteArray(), StandardCharsets.UTF_8);
        } catch (Exception e) {
            return "";
        }
    }

    @JavascriptInterface
    public void toast(final String msg) {
        ui.post(() -> Toast.makeText(act, msg, Toast.LENGTH_SHORT).show());
    }

    @JavascriptInterface
    public void exitApp() {
        ui.post(act::finish);
    }

    @JavascriptInterface
    public void login(final String rid, final String username, final String password, final String label) {
        new Thread(() -> {
            try {
                JSONObject vars = new JSONObject();
                vars.put("u", username);
                vars.put("p", password);
                Api.Resp r = Api.gql(null, Api.Q_LOGIN, vars);
                JSONObject body = r.json();
                JSONObject login = body == null ? null : body.optJSONObject("data") == null
                        ? null : body.optJSONObject("data").optJSONObject("login");
                if (login == null) {
                    fail(rid, body == null ? "登录失败（HTTP " + r.code + "）" : firstError(body, "登录失败"));
                    return;
                }
                String access = login.optString("accessToken");
                String refresh = login.optString("refreshToken");
                JSONObject acc = new JSONObject();
                acc.put("key", "u_" + username);
                acc.put("username", username);
                acc.put("password", password);
                acc.put("label", label == null || label.isEmpty() ? username : label);
                acc.put("access", access);
                acc.put("refresh", refresh);
                acc.put("userId", uidOf(access));

                Api.Resp me = Api.gql(access, Api.Q_ME, null);
                JSONObject meJson = me.json();
                JSONObject viewer = meJson != null && meJson.optJSONObject("data") != null
                        ? meJson.optJSONObject("data").optJSONObject("viewer") : null;
                if (viewer != null) {
                    JSONObject user = viewer.optJSONObject("user");
                    if (user != null) {
                        acc.put("userId", user.optString("id", acc.optString("userId")));
                        acc.put("username", user.optString("username", username));
                        JSONObject profile = user.optJSONObject("profile");
                        acc.put("nickname", profile == null ? "" : profile.optString("nickname", ""));
                        acc.put("institute", profile == null ? "" : profile.optString("institute", ""));
                    }
                }
                synchronized (ioLock) {
                    JSONObject all = accounts();
                    all.put(acc.optString("key"), acc);
                    saveAccounts(all);
                }
                JSONObject out = new JSONObject();
                out.put("ok", true);
                out.put("account", acc);
                respond(rid, out);
            } catch (Exception e) {
                fail(rid, "登录异常：" + e.getMessage());
            }
        }).start();
    }

    @JavascriptInterface
    public void gql(final String rid, final String acctKey, final String query, final String varsJson) {
        new Thread(() -> {
            try {
                JSONObject all = accounts();
                JSONObject acc = all.optJSONObject(acctKey);
                if (acc == null) {
                    fail(rid, "账号不存在，请重新登录");
                    return;
                }
                JSONObject vars = new JSONObject(varsJson == null || varsJson.isEmpty() ? "{}" : varsJson);
                Api.Resp r = Api.gql(acc.optString("access"), query, vars);
                JSONObject body = r.json();
                if (needAuth(r, body)) {
                    if (!refreshToken(acc)) {
                        fail(rid, "登录已过期，请重新登录该账号");
                        return;
                    }
                    r = Api.gql(acc.optString("access"), query, vars);
                    body = r.json();
                }
                if (body == null) {
                    fail(rid, "返回异常（HTTP " + r.code + "）");
                    return;
                }
                JSONObject out = new JSONObject();
                out.put("ok", true);
                out.put("http", r.code);
                out.put("body", body);
                respond(rid, out);
            } catch (Exception e) {
                fail(rid, "请求异常：" + e.getMessage());
            }
        }).start();
    }

    @JavascriptInterface
    public void logout(final String acctKey) {
        synchronized (ioLock) {
            JSONObject all = accounts();
            all.remove(acctKey);
            saveAccounts(all);
        }
    }

    // ---------- 内部 ----------

    private boolean needAuth(Api.Resp r, JSONObject body) {
        if (r.code == 401 || r.code == 403) return true;
        if (body == null) return false;
        String err = firstError(body, "");
        return err.contains("未登录") || err.contains("登录已过期") || err.contains("Unauthorized");
    }

    private boolean refreshToken(JSONObject acc) {
        synchronized (ioLock) {
            try {
                String rt = acc.optString("refresh");
                if (rt.isEmpty()) return false;
                JSONObject vars = new JSONObject();
                vars.put("rt", rt);
                Api.Resp r = Api.gql(null, Api.Q_REFRESH, vars);
                JSONObject body = r.json();
                JSONObject login = body == null || body.optJSONObject("data") == null
                        ? null : body.optJSONObject("data").optJSONObject("login");
                if (login == null) return false;
                acc.put("access", login.optString("accessToken"));
                acc.put("refresh", login.optString("refreshToken"));
                JSONObject all = accounts();
                all.put(acc.optString("key"), acc);
                saveAccounts(all);
                return true;
            } catch (Exception e) {
                return false;
            }
        }
    }

    private String uidOf(String jwt) {
        try {
            String part = jwt.split("\\.")[1];
            byte[] raw = Base64.decode(part, Base64.URL_SAFE | Base64.NO_WRAP | Base64.NO_PADDING);
            JSONObject p = new JSONObject(new String(raw, StandardCharsets.UTF_8));
            String gid = "User:" + p.optLong("uid");
            return Base64.encodeToString(gid.getBytes(StandardCharsets.UTF_8),
                    Base64.NO_WRAP | Base64.NO_PADDING);
        } catch (Exception e) {
            return "";
        }
    }

    private String firstError(JSONObject body, String def) {
        try {
            if (body.has("errors")) {
                return body.getJSONArray("errors").getJSONObject(0).optString("message", def);
            }
        } catch (Exception ignored) {
        }
        return def;
    }

    private void fail(String rid, String msg) {
        try {
            JSONObject out = new JSONObject();
            out.put("ok", false);
            out.put("error", msg);
            respond(rid, out);
        } catch (Exception ignored) {
        }
    }

    private void respond(final String rid, final JSONObject payload) {
        ui.post(() -> {
            String js = "window.__szResolve(" + JSONObject.quote(rid) + "," + JSONObject.quote(payload.toString()) + ")";
            web.evaluateJavascript(js, null);
        });
    }
}
