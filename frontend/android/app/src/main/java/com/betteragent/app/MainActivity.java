package com.betteragent.app;

import android.content.Intent;
import android.os.Bundle;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        // In-app APK self-update (download + package-installer intent).
        registerPlugin(ApkUpdaterPlugin.class);
        super.onCreate(savedInstanceState);
    }

    // MainActivity is launchMode="singleTask", so a share that arrives
    // while the app is already running is delivered via onNewIntent
    // rather than a fresh onCreate. Capacitor (and the send-intent
    // plugin) read the *current* intent, so swap it in here — otherwise
    // a warm-resume share would be read from the stale launch intent.
    @Override
    public void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
    }
}
