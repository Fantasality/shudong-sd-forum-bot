package com.shudong.xray;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/** 鼠洞 SD GraphQL 传输层：只做 HTTP，不含业务。 */
final class Api {
    static final String ENDPOINT = "https://api.szlikeyou.com/graphql";
    static final String UA =
            "Mozilla/5.0 (Linux; Android 14; Pixel 7 Build/UQ1A.240105.002; wv) AppleWebKit/537.36 "
                    + "(KHTML, like Gecko) Version/4.0 Chrome/122.0.6261.119 Mobile Safari/537.36 "
                    + "MicroMessenger/8.0.49.2600(0x28003133) WeChat/arm64 Weixin NetType/WIFI "
                    + "Language/zh_CN ABI/arm64 MiniProgramEnv/android";

    static final String Q_LOGIN =
            "mutation L($u:String!,$p:String!){login(input:{basicInput:{username:$u,password:$p}})"
                    + "{accessToken refreshToken __typename}}";
    static final String Q_REFRESH =
            "mutation R($rt:String!){login(input:{refreshTokenInput:{refreshToken:$rt}})"
                    + "{accessToken refreshToken __typename}}";
    static final String Q_ME =
            "query{viewer{id user{id username profile{nickname institute}}}}";

    private Api() {
    }

    static final class Resp {
        final int code;
        final String body;

        Resp(int code, String body) {
            this.code = code;
            this.body = body == null ? "" : body;
        }

        boolean httpOk() {
            return code >= 200 && code < 300;
        }

        JSONObject json() {
            try {
                return new JSONObject(body);
            } catch (Exception e) {
                return null;
            }
        }
    }

    static Resp post(String token, String payload) {
        HttpURLConnection conn = null;
        try {
            URL url = new URL(ENDPOINT);
            conn = (HttpURLConnection) url.openConnection();
            conn.setRequestMethod("POST");
            conn.setConnectTimeout(15000);
            conn.setReadTimeout(25000);
            conn.setDoOutput(true);
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            conn.setRequestProperty("Accept", "application/json");
            conn.setRequestProperty("User-Agent", UA);
            conn.setRequestProperty("Origin", "https://servicewechat.com");
            conn.setRequestProperty("Referer", "https://servicewechat.com/");
            if (token != null && !token.isEmpty()) {
                conn.setRequestProperty("Authorization", "Bearer " + token);
            }
            byte[] raw = payload.getBytes(StandardCharsets.UTF_8);
            conn.setFixedLengthStreamingMode(raw.length);
            try (OutputStream os = conn.getOutputStream()) {
                os.write(raw);
            }
            int code = conn.getResponseCode();
            InputStream is = code >= 400 ? conn.getErrorStream() : conn.getInputStream();
            return new Resp(code, readAll(is));
        } catch (Exception e) {
            return new Resp(0, "{\"errors\":[{\"message\":\"网络连接失败：" + escape(e.getMessage()) + "\"}]}");
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    static Resp gql(String token, String query, JSONObject variables) {
        JSONObject body = new JSONObject();
        try {
            body.put("query", query);
            body.put("variables", variables == null ? new JSONObject() : variables);
        } catch (Exception ignored) {
            return new Resp(0, "{\"errors\":[{\"message\":\"请求构造失败\"}]}");
        }
        return post(token, body.toString());
    }

    private static String readAll(InputStream is) {
        if (is == null) return "";
        try (InputStream in = is; ByteArrayOutputStream bos = new ByteArrayOutputStream()) {
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
            return new String(bos.toByteArray(), StandardCharsets.UTF_8);
        } catch (Exception e) {
            return "";
        }
    }

    private static String escape(String s) {
        if (s == null) return "";
        return s.replace("\\", "\\\\").replace("\"", "'").replace("\n", " ");
    }
}
