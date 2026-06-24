package com.betteragent.app;

import android.content.Intent;
import android.net.Uri;

import androidx.core.content.FileProvider;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * In-app APK self-update for the native build. Downloads a new APK from
 * the backend (carrying the bearer token the auth gate requires) into app
 * cache, then hands it to Android's package installer via a FileProvider
 * URI. Android still shows its own Install confirmation — the OS does not
 * let any app silently replace itself, so this is as seamless as Android
 * allows. Requires the REQUEST_INSTALL_PACKAGES permission.
 */
@CapacitorPlugin(name = "ApkUpdater")
public class ApkUpdaterPlugin extends Plugin {

    @PluginMethod
    public void downloadAndInstall(PluginCall call) {
        String urlStr = call.getString("url");
        String token = call.getString("token");
        if (urlStr == null) {
            call.reject("url is required");
            return;
        }

        final File out = new File(getContext().getCacheDir(), "apk-updates/update.apk");
        if (out.getParentFile() != null) out.getParentFile().mkdirs();

        new Thread(() -> {
            HttpURLConnection conn = null;
            try {
                conn = (HttpURLConnection) new URL(urlStr).openConnection();
                if (token != null && !token.isEmpty()) {
                    conn.setRequestProperty("Authorization", "Bearer " + token);
                }
                conn.setConnectTimeout(30_000);
                conn.setReadTimeout(300_000);
                conn.setInstanceFollowRedirects(true);
                int status = conn.getResponseCode();
                if (status != HttpURLConnection.HTTP_OK) {
                    call.reject("download failed: HTTP " + status);
                    return;
                }
                try (InputStream in = conn.getInputStream();
                     OutputStream os = new FileOutputStream(out)) {
                    byte[] buf = new byte[8192];
                    int n;
                    while ((n = in.read(buf)) > 0) os.write(buf, 0, n);
                }

                getActivity().runOnUiThread(this::launchInstaller);
                JSObject ret = new JSObject();
                ret.put("path", out.getAbsolutePath());
                call.resolve(ret);
            } catch (Exception e) {
                call.reject("download failed: " + e.getMessage());
            } finally {
                if (conn != null) conn.disconnect();
            }
        }).start();
    }

    private void launchInstaller() {
        try {
            Uri uri = FileProvider.getUriForFile(
                    getContext(),
                    getContext().getPackageName() + ".fileprovider",
                    new File(getContext().getCacheDir(), "apk-updates/update.apk"));
            Intent intent = new Intent(Intent.ACTION_VIEW)
                    .setDataAndType(uri, "application/vnd.android.package-archive")
                    .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_GRANT_READ_URI_PERMISSION);
            getActivity().startActivity(intent);
        } catch (Exception e) {
            // Non-fatal — the download already succeeded; the user can
            // retry from the popup. Logged via the plugin call elsewhere.
        }
    }
}
