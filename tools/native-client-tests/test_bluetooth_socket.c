/* Execute the production RFCOMM parser/open helper using deterministic WinSock
 * results. SDK declarations are included before API remapping. No network DLL
 * is linked: any accidental, unmocked network call must fail the build.
 */
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2bth.h>
#include <windows.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static unsigned checks, scenarios;
#define CHECK(value) do { ++checks; if (!(value)) { \
 fprintf(stderr,"FAIL %s:%d: %s\n",__FILE__,__LINE__,#value); exit(1); \
} } while (0)
#define TEST_SOCKET ((SOCKET)(uintptr_t)0x12345)
#define TEST_ADDR ((BTH_ADDR)UINT64_C(0x12AB34CD56EF))

/* GUID_NULL is SDK data rather than a networking API. Supply its documented
 * all-zero value locally so tests need no UUID or WinSock import library. */
const GUID GUID_NULL = {0,0,0,{0,0,0,0,0,0,0,0}};

typedef struct {
 int startup_error, socket_error, nonblocking_error, blocking_error;
 int connect_error, select_error, getsockopt_error, socket_status_error;
 int select_result, socket_status_length;
 bool write_ready, exception_ready;
 unsigned startup_calls, cleanup_calls, socket_calls, close_calls;
 unsigned ioctl_calls, connect_calls, select_calls, getsockopt_calls;
 unsigned references;
 bool socket_open, nonblocking;
 int last_error;
 DWORD timeout_ms;
} mock_t;
static mock_t mock;

static void reset(void) {
 CHECK(mock.references==0 && !mock.socket_open);
 memset(&mock,0,sizeof(mock));
 mock.timeout_ms=2567;
 mock.select_result=1;mock.write_ready=true;
 mock.socket_status_length=(int)sizeof(int);
 ++scenarios;
}
static bool contains(const fd_set *set,SOCKET value) {
 if (!set) return false;
 for (u_int i=0;i<set->fd_count;++i) if (set->fd_array[i]==value) return true;
 return false;
}
static int WSAAPI fake_WSAStartup(WORD version,LPWSADATA data) {
 CHECK(version==MAKEWORD(2,2) && data && mock.references==0);
 ++mock.startup_calls;
 if (mock.startup_error) return mock.startup_error;
 memset(data,0,sizeof(*data));data->wVersion=MAKEWORD(2,2);data->wHighVersion=MAKEWORD(2,2);
 ++mock.references;return 0;
}
static int WSAAPI fake_WSACleanup(void) {
 CHECK(mock.references==1 && !mock.socket_open);
 ++mock.cleanup_calls;--mock.references;
 mock.last_error=WSANOTINITIALISED;return 0;
}
static int WSAAPI fake_WSAGetLastError(void) { return mock.last_error; }
static SOCKET WSAAPI fake_socket(int af,int type,int protocol) {
 CHECK(mock.references==1 && !mock.socket_open);
 CHECK(af==AF_BTH && type==SOCK_STREAM && protocol==BTHPROTO_RFCOMM);
 ++mock.socket_calls;
 if (mock.socket_error) {mock.last_error=mock.socket_error;return INVALID_SOCKET;}
 mock.socket_open=true;return TEST_SOCKET;
}
static int WSAAPI fake_closesocket(SOCKET value) {
 CHECK(value==TEST_SOCKET && mock.socket_open && mock.references==1);
 ++mock.close_calls;mock.socket_open=false;mock.last_error=WSAENOTSOCK;return 0;
}
static int WSAAPI fake_ioctlsocket(SOCKET value,long command,u_long *mode) {
 CHECK(value==TEST_SOCKET && mock.socket_open && mode && command==(long)FIONBIO);
 CHECK(*mode==0 || *mode==1);++mock.ioctl_calls;
 if (*mode==1) {
  CHECK(!mock.nonblocking && mock.connect_calls==0);
  if (mock.nonblocking_error) {mock.last_error=mock.nonblocking_error;return SOCKET_ERROR;}
  mock.nonblocking=true;
 } else {
  CHECK(mock.nonblocking && mock.connect_calls==1);
  if (mock.blocking_error) {mock.last_error=mock.blocking_error;return SOCKET_ERROR;}
  mock.nonblocking=false;
 }
 return 0;
}
static int WSAAPI fake_connect(SOCKET value,const struct sockaddr *address,int length) {
 const SOCKADDR_BTH *bt=(const SOCKADDR_BTH *)(const void *)address;
 static const GUID zero={0,0,0,{0,0,0,0,0,0,0,0}};
 CHECK(value==TEST_SOCKET && mock.references==1 && mock.socket_open && mock.nonblocking);
 CHECK(address && length==(int)sizeof(SOCKADDR_BTH));
 CHECK(bt->addressFamily==AF_BTH && bt->btAddr==TEST_ADDR && bt->port==1);
 CHECK(memcmp(&bt->serviceClassId,&zero,sizeof(zero))==0);
 ++mock.connect_calls;
 if (mock.connect_error) {mock.last_error=mock.connect_error;return SOCKET_ERROR;}
 return 0;
}
static int WSAAPI fake_select(int nfds,fd_set *read,fd_set *write,fd_set *exception,const struct timeval *timeout) {
 CHECK(nfds==0 && !read && write && exception && timeout);
 CHECK(mock.socket_open && mock.nonblocking && mock.connect_calls==1);
 CHECK(contains(write,TEST_SOCKET) && contains(exception,TEST_SOCKET));
 CHECK(timeout->tv_sec==(long)(mock.timeout_ms/1000));
 CHECK(timeout->tv_usec==(long)((mock.timeout_ms%1000)*1000));
 ++mock.select_calls;CHECK(mock.select_calls==1);
 if (mock.select_result==SOCKET_ERROR) {mock.last_error=mock.select_error;return SOCKET_ERROR;}
 FD_ZERO(write);FD_ZERO(exception);
 if (mock.select_result>0) {
  if (mock.write_ready) FD_SET(TEST_SOCKET,write);
  if (mock.exception_ready) FD_SET(TEST_SOCKET,exception);
 }
 return mock.select_result;
}
static int WSAAPI fake_getsockopt(SOCKET value,int level,int option,char *data,int *length) {
 CHECK(value==TEST_SOCKET && mock.socket_open && mock.nonblocking && mock.select_calls==1);
 CHECK(level==SOL_SOCKET && option==SO_ERROR && data && length && *length==(int)sizeof(int));
 ++mock.getsockopt_calls;
 if (mock.getsockopt_error) {mock.last_error=mock.getsockopt_error;return SOCKET_ERROR;}
 memcpy(data,&mock.socket_status_error,sizeof(int));*length=mock.socket_status_length;return 0;
}
static int WSAAPI fake_FDIsSet(SOCKET value,fd_set *set) {
 CHECK(value==TEST_SOCKET && set);return contains(set,value) ? 1 : 0;
}
#define WSAStartup fake_WSAStartup
#define WSACleanup fake_WSACleanup
#define WSAGetLastError fake_WSAGetLastError
#define socket fake_socket
#define closesocket fake_closesocket
#define ioctlsocket fake_ioctlsocket
#define connect fake_connect
#define select fake_select
#define getsockopt fake_getsockopt
#undef FD_ISSET
#define FD_ISSET(value,set) fake_FDIsSet((value),(set))
#include "uart_win32_bluetooth.h"

static int open_fixture(SOCKET *out) {
 return pm3_bth_open("bt:12:ab:34:CD:56:ef",mock.timeout_ms,out);
}
static void failure(int expected,unsigned sockets_created) {
 SOCKET out=TEST_SOCKET;
 int result=open_fixture(&out);
 CHECK(result!=0 && (expected==0 || result==expected));
 CHECK(out==INVALID_SOCKET);
 CHECK(mock.references==0 && !mock.socket_open);
 CHECK(mock.startup_calls==1);
 CHECK(mock.close_calls==sockets_created);
 CHECK(mock.cleanup_calls==(mock.startup_error ? 0u : 1u));
}
static void success(void) {
 SOCKET out=INVALID_SOCKET;
 CHECK(open_fixture(&out)==0 && out==TEST_SOCKET);
 CHECK(mock.references==1 && mock.socket_open && !mock.nonblocking);
 CHECK(mock.startup_calls==1 && mock.cleanup_calls==0 && mock.close_calls==0);
 CHECK(mock.ioctl_calls==2 && mock.connect_calls==1);
 /* The successful socket/WSA reference belongs to the caller. */
 CHECK(fake_closesocket(out)==0);CHECK(fake_WSACleanup()==0);
 CHECK(mock.references==0 && !mock.socket_open && mock.close_calls==1 && mock.cleanup_calls==1);
}
static void parser(void) {
 static const struct { const char *text;BTH_ADDR address; } valid[]={
  {"bt:12ab34CD56ef",TEST_ADDR},{"BT:12AB34CD56EF",TEST_ADDR},
  {"bT:12:Ab:34:cD:56:ef",TEST_ADDR},{"Bt:00:00:00:00:00:01",(BTH_ADDR)1},
  {"bt:000000000001",(BTH_ADDR)1},{"bt:FFFFFFFFFFFE",(BTH_ADDR)UINT64_C(0xFFFFFFFFFFFE)}
 };
 static const char *invalid[]={
  "", "bt:", "bt:000000000000", "bt:FF:FF:FF:FF:FF:FF", "bt:FFFFFFFFFFFF",
  "bt:00000000000", "bt:0000000000001", "bt:12ab34cd56eg", "bt:12ab34cd56ef ",
  " bt:12ab34cd56ef", "spp:12ab34cd56ef", "btx:12ab34cd56ef", "bt:0x12ab34cd56ef",
  "bt:12-ab-34-cd-56-ef", "bt:1:ab:34:cd:56:ef", "bt:12::34:cd:56:ef",
  "bt:12:ab:34:cd:56:ef:01", "bt:12ab34cd56ef:1", "bt:12:ab:34:cd:56",
  "bt:12:ab:34:cd:56:ef\n", "bt:12ab34cd56ef;hw version", "bt:\xff\xff\xff\xff\xff\xff"
 };
 BTH_ADDR address;
 reset();
 for (size_t i=0;i<sizeof(valid)/sizeof(valid[0]);++i) {
  address=0;CHECK(pm3_bth_parse_endpoint(valid[i].text,&address));CHECK(address==valid[i].address);
 }
 for (size_t i=0;i<sizeof(invalid)/sizeof(invalid[0]);++i) CHECK(!pm3_bth_parse_endpoint(invalid[i],&address));
 CHECK(!pm3_bth_parse_endpoint(NULL,&address));CHECK(!pm3_bth_parse_endpoint(valid[0].text,NULL));
 CHECK(mock.startup_calls==0 && mock.socket_calls==0);
}
static void invalid_open_arguments(void) {
 static const DWORD invalid_timeouts[]={0,60001,MAXDWORD};
 SOCKET out=TEST_SOCKET;
 reset();CHECK(pm3_bth_open("bt:000000000000",1000,&out)!=0);CHECK(out==INVALID_SOCKET);
 CHECK(mock.startup_calls==0 && mock.socket_calls==0);
 reset();CHECK(pm3_bth_open(NULL,1000,&out)!=0);CHECK(out==INVALID_SOCKET);CHECK(mock.startup_calls==0);
 reset();CHECK(pm3_bth_open("bt:12ab34cd56ef",1000,NULL)!=0);CHECK(mock.startup_calls==0);
 for (size_t i=0;i<sizeof(invalid_timeouts)/sizeof(invalid_timeouts[0]);++i) {
  reset();out=TEST_SOCKET;CHECK(pm3_bth_open("bt:12ab34cd56ef",invalid_timeouts[i],&out)!=0);
  CHECK(out==INVALID_SOCKET && mock.startup_calls==0 && mock.socket_calls==0);
 }
}
static void resources_and_errors(void) {
 reset();mock.startup_error=WSASYSNOTREADY;failure(WSASYSNOTREADY,0);CHECK(mock.socket_calls==0);
 reset();mock.socket_error=WSAEPROTONOSUPPORT;failure(WSAEPROTONOSUPPORT,0);CHECK(mock.connect_calls==0);
 reset();mock.nonblocking_error=WSAEINVAL;failure(WSAEINVAL,1);CHECK(mock.connect_calls==0);
 reset();mock.connect_error=WSAEHOSTUNREACH;failure(WSAEHOSTUNREACH,1);CHECK(mock.select_calls==0);
 reset();mock.connect_error=WSAEACCES;failure(WSAEACCES,1);CHECK(mock.select_calls==0);
 reset();mock.blocking_error=WSAEINVAL;failure(WSAEINVAL,1);
}
static void bounded_connect(void) {
 static const int pending[]={WSAEWOULDBLOCK,WSAEINPROGRESS,WSAEALREADY};
 static const DWORD timeouts[]={1,2567,60000};
 reset();success();CHECK(mock.select_calls==0 && mock.getsockopt_calls==0);
 for (size_t i=0;i<sizeof(pending)/sizeof(pending[0]);++i) {
  for (size_t j=0;j<sizeof(timeouts)/sizeof(timeouts[0]);++j) {
   reset();mock.connect_error=pending[i];mock.timeout_ms=timeouts[j];success();
   CHECK(mock.select_calls==1 && mock.getsockopt_calls==1);
  }
 }
 reset();mock.connect_error=WSAEWOULDBLOCK;mock.select_result=0;failure(WSAETIMEDOUT,1);CHECK(mock.getsockopt_calls==0);
 reset();mock.connect_error=WSAEWOULDBLOCK;mock.select_result=SOCKET_ERROR;mock.select_error=WSAENETDOWN;failure(WSAENETDOWN,1);CHECK(mock.getsockopt_calls==0);
 reset();mock.connect_error=WSAEINPROGRESS;mock.select_result=SOCKET_ERROR;mock.select_error=WSAEINTR;failure(WSAEINTR,1);
 reset();mock.connect_error=WSAEWOULDBLOCK;mock.getsockopt_error=WSAENOPROTOOPT;failure(WSAENOPROTOOPT,1);
 reset();mock.connect_error=WSAEWOULDBLOCK;mock.socket_status_error=WSAECONNREFUSED;failure(WSAECONNREFUSED,1);
 reset();mock.connect_error=WSAEWOULDBLOCK;mock.socket_status_error=WSAEACCES;mock.write_ready=false;mock.exception_ready=true;failure(WSAEACCES,1);
 reset();mock.connect_error=WSAEWOULDBLOCK;mock.socket_status_length=(int)sizeof(int)-1;failure(0,1);
 reset();mock.connect_error=WSAEWOULDBLOCK;mock.socket_status_length=(int)sizeof(int)+1;failure(0,1);
 reset();mock.connect_error=WSAEWOULDBLOCK;mock.write_ready=false;mock.exception_ready=true;failure(0,1);
 reset();mock.connect_error=WSAEWOULDBLOCK;mock.write_ready=true;mock.exception_ready=true;mock.select_result=2;failure(0,1);
 reset();mock.connect_error=WSAEWOULDBLOCK;mock.write_ready=false;mock.exception_ready=false;failure(0,1);
 reset();mock.connect_error=WSAEWOULDBLOCK;mock.blocking_error=WSAEINVAL;failure(WSAEINVAL,1);
}
int main(void) {
 parser();invalid_open_arguments();resources_and_errors();bounded_connect();
 CHECK(mock.references==0 && !mock.socket_open);
 printf("bluetooth socket helper: %u scenarios, %u assertions passed; hardware API calls mocked\n",scenarios,checks);
 return 0;
}
