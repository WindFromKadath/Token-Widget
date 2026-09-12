({
  request: {
    url: "http://127.0.0.1:18443/usage",
    method: "GET",
    headers: {
      "User-Agent": "cc-switch/1.0"
    }
  },
  extractor: function (response) {
    if (!response.success) {
      return {
        isValid: false,
        invalidMessage: response.data || response.reason || "桥接服务查询失败"
      };
    }
    var u = response.usage || {};
    var rolling = u.rolling || {};
    return {
      planName: "OpenCode Go",
      used: rolling.percent,
      remaining: 100 - rolling.percent,
      total: 100,
      unit: "%",
      extra: response.data
    };
  }
})
