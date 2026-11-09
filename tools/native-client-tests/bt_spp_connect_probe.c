/* Diagnostic only: connect the explicitly selected paired SPP service and close.
 * No application bytes, PM3 commands, pairing or service-state changes.
 */
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2bth.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static int discover_spp(SOCKADDR_BTH *address) {
    char context[128]; DWORD characters=sizeof(context);
    WSAQUERYSETA query; HANDLE lookup=NULL;
    DWORD flags=LUP_RETURN_ADDR|LUP_RETURN_NAME|LUP_FLUSHCACHE;
    DWORD bytes=8192; WSAQUERYSETA *answer; int error=0,channel=0;
    SOCKADDR_BTH printable=*address;
    memset(&printable.serviceClassId,0,sizeof(printable.serviceClassId));
    if(WSAAddressToStringA((SOCKADDR*)&printable,sizeof(printable),NULL,context,&characters)) {
        printf("{\"sdp_format_error\":%d,\"sockaddr_bytes\":%zu}\n",WSAGetLastError(),sizeof(printable)); return 1;
    }
    memset(&query,0,sizeof(query)); query.dwSize=sizeof(query);
    query.dwNameSpace=NS_BTH; query.lpServiceClassId=&address->serviceClassId;
    query.lpszContext=context;
    if(WSALookupServiceBeginA(&query,flags,&lookup)) {
        printf("{\"sdp_begin_error\":%d,\"context\":\"%s\"}\n",WSAGetLastError(),context); return 1;
    }
    answer=calloc(1,bytes);
    if(!answer) { WSALookupServiceEnd(lookup); return 1; }
    answer->dwSize=sizeof(*answer);
    if(WSALookupServiceNextA(lookup,flags,&bytes,answer)) error=WSAGetLastError();
    else if(answer->dwNumberOfCsAddrs && answer->lpcsaBuffer &&
            answer->lpcsaBuffer[0].RemoteAddr.iSockaddrLength>= (int)sizeof(SOCKADDR_BTH))
        channel=(int)((SOCKADDR_BTH*)answer->lpcsaBuffer[0].RemoteAddr.lpSockaddr)->port;
    printf("{\"sdp_lookup_error\":%d,\"rfcomm_channel\":%d,\"context\":\"%s\",\"application_bytes_sent\":0,\"pairing_changed\":false}\n",error,channel,context);
    free(answer); WSALookupServiceEnd(lookup); return error?1:0;
}
int main(int argc, char **argv) {
    WSADATA data; SOCKADDR_BTH address; SOCKET sock; u_long nonblocking=1;
    fd_set writable,errors; struct timeval limit={10,0};
    int status,error=0,length=sizeof(error),connected=0; char *end;
    if((argc!=2 && argc!=3) || strlen(argv[1])!=12 || strspn(argv[1],"0123456789abcdefABCDEF")!=12) return 2;
    memset(&address,0,sizeof(address));
    address.addressFamily=AF_BTH; address.btAddr=strtoull(argv[1],&end,16);
    if(*end) return 2;
    address.serviceClassId=(GUID){0x1101,0,0x1000,{0x80,0,0,0x80,0x5f,0x9b,0x34,0xfb}};
    status=WSAStartup(MAKEWORD(2,2),&data);
    if(status) { printf("{\"startup_error\":%d}\n",status); return 1; }
    if(argc==3) {
        status=strcmp(argv[2],"--sdp") ? 2 : discover_spp(&address);
        WSACleanup(); return status;
    }
    sock=socket(AF_BTH,SOCK_STREAM,BTHPROTO_RFCOMM);
    if(sock==INVALID_SOCKET) { error=WSAGetLastError(); goto done; }
    if(ioctlsocket(sock,FIONBIO,&nonblocking)) { error=WSAGetLastError(); goto close_socket; }
    status=connect(sock,(SOCKADDR*)&address,sizeof(address));
    if(status==0) connected=1;
    else {
        error=WSAGetLastError();
        if(error==WSAEWOULDBLOCK || error==WSAEINPROGRESS) {
            FD_ZERO(&writable); FD_ZERO(&errors); FD_SET(sock,&writable); FD_SET(sock,&errors);
            status=select(0,NULL,&writable,&errors,&limit);
            if(status==0) error=WSAETIMEDOUT;
            else if(status<0) error=WSAGetLastError();
            else if(getsockopt(sock,SOL_SOCKET,SO_ERROR,(char*)&error,&length)) error=WSAGetLastError();
            else connected=error==0;
        }
    }
close_socket:
    if(connected) shutdown(sock,SD_BOTH);
    closesocket(sock);
done:
    WSACleanup();
    printf("{\"spp_socket_connected\":%s,\"wsa_error\":%d,\"application_bytes_sent\":0,\"socket_closed\":true,\"pairing_changed\":false}\n",connected?"true":"false",error);
    return connected?0:1;
}
