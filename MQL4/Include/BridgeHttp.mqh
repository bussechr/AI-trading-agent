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
#import

// Import GetLastError from kernel32
#import "kernel32.dll"
int GetLastError();
#import

// Constants
#define INTERNET_OPEN_TYPE_PRECONFIG 0
#define INTERNET_FLAG_RELOAD 0x80000000
#define INTERNET_FLAG_NO_CACHE_WRITE 0x04000000
#define INTERNET_FLAG_PRAGMA_NOCACHE 0x00000100
#define INTERNET_SERVICE_HTTP 3
#define HTTP_QUERY_STATUS_CODE 19

// Global Session Handle
int gSession = 0;

// Initialize WinInet Session
bool InitBridgeHttp(string userAgent) {
    if(!IsDllsAllowed()) {
        Alert("Error: 'Allow DLL imports' must be enabled!");
        return false;
    }
    gSession = InternetOpenW(userAgent, INTERNET_OPEN_TYPE_PRECONFIG, NULL, NULL, 0);
    if(gSession == 0) {
        Print("Error: InternetOpenW failed. Err=", kernel32::GetLastError());
        return false;
    }
    return true;
}

// Cleanup
void DeinitBridgeHttp() {
    // MT4 handles cleanup usually, but good practice if needed manually
    // if(gSession > 0) InternetCloseHandle(gSession);
    gSession = 0;
}

// Helper: POST Request
void HttpPOST(string fullUrl, string data) {
    if(IsStopped()) return;
    if(gSession == 0) return;
    
    // Parse URL (e.g., http://127.0.0.1:58710/tick)
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
   
    string headers = "Content-Type: application/json";
    uchar postData[];
    int len = StringToCharArray(data, postData, 0, WHOLE_ARRAY);
    int dataLen = len;
    if(len > 0 && postData[len-1] == 0) dataLen--; // remove null terminator
   
    if(!HttpSendRequestW(hRequest, headers, StringLen(headers), postData, dataLen)) {
        Print("HttpPOST: SendRequest failed. Err=", kernel32::GetLastError());
    }
   
    InternetCloseHandle(hRequest);
    InternetCloseHandle(hConnect);
}

// Helper: GET Request
string HttpGET(string fullUrl) {
    if(IsStopped()) return "";
    if(gSession == 0) return "";
    
    int hURL = InternetOpenUrlW(gSession, fullUrl, NULL, 0, INTERNET_FLAG_RELOAD | INTERNET_FLAG_NO_CACHE_WRITE, 0);
    if(hURL == 0) { 
       Print("HttpGET: OpenUrl failed for ", fullUrl, " Err=", kernel32::GetLastError()); 
       return ""; 
    }
    
    uchar buffer[1024];
    int bytesRead = 0;
    string result = "";
    
    while(InternetReadFile(hURL, buffer, 1024, bytesRead)) {
       if(bytesRead <= 0) break;
       result += CharArrayToString(buffer, 0, bytesRead);
    }
    
    InternetCloseHandle(hURL);
    return result;
}
