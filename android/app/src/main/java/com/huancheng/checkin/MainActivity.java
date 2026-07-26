package com.huancheng.checkin;

import android.os.Bundle;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import androidx.appcompat.app.AppCompatActivity;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

public class MainActivity extends AppCompatActivity {
    private WebView webView;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        // 启动 Python 后端
        if (!Python.isStarted()) {
            Python.start(new AndroidPlatform(this));
        }

        new Thread(() -> {
            try {
                Python py = Python.getInstance();
                py.getModule("web_app").callAttr("start_server");
            } catch (Exception e) {
                e.printStackTrace();
            }
        }).start();

        // WebView 加载本地服务
        webView = findViewById(R.id.webview);
        webView.getSettings().setJavaScriptEnabled(true);
        webView.setWebViewClient(new WebViewClient());
        webView.loadUrl("http://127.0.0.1:5000");
    }

    @Override
    public void onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }
}
