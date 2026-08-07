//+------------------------------------------------------------------+
//|                                                   BridgeHttp.mqh |
//|                                  Copyright 2024, Trading Agent |
//|                                        https://www.google.com |
//+------------------------------------------------------------------+
#property copyright "Trading Agent"
#property link      "https://www.google.com"
#property strict

// WinInet Imports
#import "wininet.dll"
int InternetOpenW(string sAgent, int lAccessType, string sProxyName, string sProxyBypass, int lFlags);
int InternetOpenUrlW(int hInternet, string sUrl, string sHeaders, int lHeadersLength, int lFlags, int lContext);
int InternetReadFile(int hFile, uchar &sBuffer[], int lNumBytesToRead, int &lNumberOfBytesRead);
int InternetCloseHandle(int hInet);
int InternetConnectW(int hInternet, string lpszServerName, int nServerPort, string lpszUserName, string lpszPassword, int dwService, int dwFlags, int dwContext);
int HttpOpenRequestW(int hConnect, string lpszVerb, string lpszObjectName, string lpszVersion, string lpszReferer, int lplpszAcceptTypes, int dwFlags, int dwContext);
int HttpSendRequestW(int hRequest, string lpszHeaders, int dwHeadersLength, uchar &lpOptional[], int dwOptionalLength);
int HttpQueryInfoW(int hRequest, int dwInfoLevel, string &lpvBuffer, int &lpdwBufferLength, int &lpdwIndex);
int InternetSetOptionW(int hInternet, int dwOption, int &lpBuffer, int dwBufferLength);
#import

// Import GetLastError from kernel32
#import "kernel32.dll"
int GetLastError();
#import

// Constants
#define INTERNET_OPEN_TYPE_PRECONFIG 0
#define INTERNET_FLAG_RELOAD -2147483648
#define INTERNET_FLAG_NO_CACHE_WRITE 0x04000000
#define INTERNET_FLAG_PRAGMA_NOCACHE 0x00000100
#define INTERNET_SERVICE_HTTP 3
#define HTTP_QUERY_STATUS_CODE 19
#define INTERNET_OPTION_CONNECT_TIMEOUT 2
#define INTERNET_OPTION_SEND_TIMEOUT 5
#define INTERNET_OPTION_RECEIVE_TIMEOUT 6
#define BRIDGE_HTTP_CONNECT_TIMEOUT_MS 250
#define BRIDGE_HTTP_SEND_TIMEOUT_MS 750
#define BRIDGE_HTTP_RECEIVE_TIMEOUT_MS 750
#define BRIDGE_HTTP_WEBREQUEST_TIMEOUT_MS 1500
#define BRIDGE_HTTP_MAX_RESPONSE_BYTES 4194304

// Global Session Handle
int gSession = 0;
bool gUseWebRequest = false;
int gLastHttpStatus = 0;

int LastBridgeHttpStatus() {
    return gLastHttpStatus;
}

string LastBridgeTransportMode() {
    return gUseWebRequest ? "webrequest" : "wininet";
}

int QueryHttpStatusCode(int hRequest) {
    string statusText = "";
    int bufLen = 64;
    int index = 0;
    if(!HttpQueryInfoW(hRequest, HTTP_QUERY_STATUS_CODE, statusText, bufLen, index)) {
        return 0;
    }
    StringTrimLeft(statusText);
    StringTrimRight(statusText);
    if(StringLen(statusText) <= 0) return 0;
    return (int)StringToInteger(statusText);
}

bool SetBridgeHttpTimeout(int handle, int option, int timeoutMs) {
    int configuredTimeout = timeoutMs;
    ResetLastError();
    return InternetSetOptionW(handle, option, configuredTimeout, 4) != 0;
}

bool ConfigureBridgeHttpTimeouts(int handle) {
    if(handle <= 0) return false;
    return
        SetBridgeHttpTimeout(
            handle, INTERNET_OPTION_CONNECT_TIMEOUT,
            BRIDGE_HTTP_CONNECT_TIMEOUT_MS
        ) &&
        SetBridgeHttpTimeout(
            handle, INTERNET_OPTION_SEND_TIMEOUT,
            BRIDGE_HTTP_SEND_TIMEOUT_MS
        ) &&
        SetBridgeHttpTimeout(
            handle, INTERNET_OPTION_RECEIVE_TIMEOUT,
            BRIDGE_HTTP_RECEIVE_TIMEOUT_MS
        );
}

// Initialize WinInet Session
bool InitBridgeHttp(string userAgent) {
    if(gSession > 0) InternetCloseHandle(gSession);
    gSession = 0;
    if(!IsDllsAllowed()) {
        gUseWebRequest = true;
        Print("BridgeHttp: DLL imports disabled, falling back to WebRequest transport.");
        return true;
    }
    gSession = InternetOpenW(userAgent, INTERNET_OPEN_TYPE_PRECONFIG, NULL, NULL, 0);
    if(gSession == 0) {
        gUseWebRequest = true;
        Print("BridgeHttp: InternetOpenW failed. Err=", kernel32::GetLastError(), " -> falling back to WebRequest transport.");
        return true;
    }
    if(!ConfigureBridgeHttpTimeouts(gSession)) {
        int timeoutError = kernel32::GetLastError();
        InternetCloseHandle(gSession);
        gSession = 0;
        gUseWebRequest = true;
        Print(
            "BridgeHttp: bounded WinInet timeouts unavailable. Err=",
            timeoutError,
            " -> falling back to WebRequest transport."
        );
        return true;
    }
    gUseWebRequest = false;
    return true;
}

// Cleanup
void DeinitBridgeHttp() {
    if(gSession > 0) InternetCloseHandle(gSession);
    gSession = 0;
    gUseWebRequest = false;
}

// Helper: POST Request
void HttpPOST(string fullUrl, string data, string apiKey="") {
    gLastHttpStatus = 0;
    if(IsStopped()) return;
    if(gUseWebRequest) {
        char postData[];
        int len = StringToCharArray(data, postData, 0, WHOLE_ARRAY);
        if(len > 0 && postData[len - 1] == 0) ArrayResize(postData, len - 1);
        char result[];
        string resultHeaders = "";
        string headers = "Content-Type: application/json\r\n";
        if(apiKey != "") headers = headers + "X-API-Key: " + apiKey + "\r\n";
        ResetLastError();
        int status = WebRequest(
            "POST", fullUrl, headers, BRIDGE_HTTP_WEBREQUEST_TIMEOUT_MS,
            postData, result, resultHeaders
        );
        gLastHttpStatus = status;
        if(status == -1) {
            Print("HttpPOST(WebRequest): failed for ", fullUrl, " Err=", GetLastError());
        } else if(ArraySize(result) > BRIDGE_HTTP_MAX_RESPONSE_BYTES) {
            gLastHttpStatus = 0;
            Print("HttpPOST(WebRequest): response exceeded bounded cap.");
        }
        return;
    }
    if(gSession == 0) return;
    
    // Parse URL (e.g., http://127.0.0.1:58710/v2/market/tick)
    string host = "127.0.0.1";
    int port = 80;
    string path = "";
    
    string url = fullUrl;
    int idx = StringFind(url, "://");
    if(idx >= 0) url = StringSubstr(url, idx + 3);
    
    // Split host:port from path
    int slashIdx = StringFind(url, "/");
    string authority = (slashIdx >= 0) ? StringSubstr(url, 0, slashIdx) : url;
    path = (slashIdx >= 0) ? StringSubstr(url, slashIdx) : "/";
    
    // Split host:port
    int colonIdx = StringFind(authority, ":");
    if(colonIdx >= 0) {
        host = StringSubstr(authority, 0, colonIdx);
        port = (int)StringToInteger(StringSubstr(authority, colonIdx + 1));
    } else {
        host = authority;
    }

    int hConnect = InternetConnectW(gSession, host, port, NULL, NULL, INTERNET_SERVICE_HTTP, 0, 0);
    if(hConnect == 0) { Print("HttpPOST: Connect failed. Err=", kernel32::GetLastError()); return; }
   
    int hRequest = HttpOpenRequestW(hConnect, "POST", path, NULL, NULL, 0, INTERNET_FLAG_RELOAD | INTERNET_FLAG_NO_CACHE_WRITE, 0);
    if(hRequest == 0) { 
        Print("HttpPOST: OpenRequest failed. Err=", kernel32::GetLastError()); 
        InternetCloseHandle(hConnect); 
        return; 
    }
   
    string headers = "Content-Type: application/json\r\n";
    if(apiKey != "") headers = headers + "X-API-Key: " + apiKey + "\r\n";
    uchar postData[];
    int len = StringToCharArray(data, postData, 0, WHOLE_ARRAY);
    int dataLen = len;
    if(len > 0 && postData[len-1] == 0) dataLen--; // remove null terminator
   
    if(!HttpSendRequestW(hRequest, headers, StringLen(headers), postData, dataLen)) {
        int sendError = kernel32::GetLastError();
        Print("HttpPOST: SendRequest failed. Err=", sendError);
        InternetCloseHandle(hRequest);
        InternetCloseHandle(hConnect);
        return;
    }
    gLastHttpStatus = QueryHttpStatusCode(hRequest);
   
    InternetCloseHandle(hRequest);
    InternetCloseHandle(hConnect);
}

// Helper: GET Request
string HttpGET(string fullUrl, string apiKey="") {
    gLastHttpStatus = 0;
    if(IsStopped()) return "";
    if(gUseWebRequest) {
        char payload[];
        ArrayResize(payload, 0);
        char result[];
        string resultHeaders = "";
        ResetLastError();
        string headers = "";
        if(apiKey != "") headers = "X-API-Key: " + apiKey + "\r\n";
        int status = WebRequest(
            "GET", fullUrl, headers, BRIDGE_HTTP_WEBREQUEST_TIMEOUT_MS,
            payload, result, resultHeaders
        );
        gLastHttpStatus = status;
        if(status == -1) {
            Print("HttpGET(WebRequest): failed for ", fullUrl, " Err=", GetLastError());
            return "";
        }
        if(ArraySize(result) > BRIDGE_HTTP_MAX_RESPONSE_BYTES) {
            gLastHttpStatus = 0;
            Print("HttpGET(WebRequest): response exceeded bounded cap.");
            return "";
        }
        return CharArrayToString(result, 0, ArraySize(result));
    }
    if(gSession == 0) return "";
    
    int hURL;
    if(apiKey != "") {
        string headers = "X-API-Key: " + apiKey + "\r\n";
        hURL = InternetOpenUrlW(gSession, fullUrl, headers, StringLen(headers), INTERNET_FLAG_RELOAD | INTERNET_FLAG_NO_CACHE_WRITE, 0);
    } else {
        hURL = InternetOpenUrlW(gSession, fullUrl, NULL, 0, INTERNET_FLAG_RELOAD | INTERNET_FLAG_NO_CACHE_WRITE, 0);
    }
    if(hURL == 0) { 
       Print("HttpGET: OpenUrl failed for ", fullUrl, " Err=", kernel32::GetLastError()); 
       gLastHttpStatus = 0;
       return ""; 
    }
    gLastHttpStatus = QueryHttpStatusCode(hURL);
    
    uchar buffer[1024];
    int bytesRead = 0;
    int totalBytes = 0;
    int responseReadError = 0;
    string result = "";
    bool responseOverflow = false;
    bool responseReadFailed = false;

    while(true) {
       if(!InternetReadFile(hURL, buffer, 1024, bytesRead)) {
          responseReadFailed = true;
          responseReadError = kernel32::GetLastError();
          break;
       }
       if(bytesRead <= 0) break;
       if(totalBytes + bytesRead > BRIDGE_HTTP_MAX_RESPONSE_BYTES) {
          responseOverflow = true;
          break;
       }
       result += CharArrayToString(buffer, 0, bytesRead);
       totalBytes += bytesRead;
    }

    InternetCloseHandle(hURL);
    if(responseOverflow) {
       gLastHttpStatus = 0;
       Print("HttpGET: response exceeded bounded cap.");
       return "";
    }
    if(responseReadFailed) {
       gLastHttpStatus = 0;
       Print("HttpGET: response read failed. Err=", responseReadError);
       return "";
    }
    return result;
}
