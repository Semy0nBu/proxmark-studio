/* Actual production BT event/read/send helpers with SDK API mocks.
 * No ws2_32 library is linked; an unmocked network call is a link failure.
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
#include <limits.h>

static unsigned checks,scenarios;
#define CHECK(v) do { ++checks; if (!(v)) {fprintf(stderr,"FAIL %s:%d: %s\n",__FILE__,__LINE__,#v);exit(1);} } while(0)
#define SOCK ((SOCKET)(uintptr_t)0x12345)
#define NETEVENT ((WSAEVENT)(uintptr_t)0x23456)
#define WAKE ((HANDLE)(uintptr_t)0x34567)

typedef struct {int count,error;DWORD elapsed;} transfer_t;
typedef struct {DWORD result,elapsed;} wait_t;
typedef struct {int result,error;long mask;int read_error,close_error;} network_t;
typedef struct {int result,error;DWORD elapsed;bool writable;} select_t;
typedef struct {
 transfer_t reads[32],sends[32];unsigned read_total,read_index,send_total,send_index;
 wait_t waits[32];unsigned wait_total,wait_index;
 network_t network[32];unsigned network_total,network_index;
 select_t selects[32];unsigned select_total,select_index;
 unsigned creates,event_selects,event_closes,resets;
 int create_error,event_select_error,last_error;bool event_open,network_signalled,wake_signalled,reset_ok;
 ULONGLONG now,started;DWORD budget;int last_recv_length,last_send_length;
 uint8_t input[128],sent[128];unsigned input_position,sent_count;
 const uint8_t *send_base;uint32_t send_length;
} mock_t;
static mock_t mock;
static void reset(void) {
 CHECK(!mock.event_open);memset(&mock,0,sizeof(mock));++scenarios;
 mock.now=mock.started=1000;mock.budget=50;mock.reset_ok=true;
 for(unsigned i=0;i<sizeof(mock.input);++i)mock.input[i]=(uint8_t)((i*37u)^0x93u);
}
static DWORD remaining(void) {ULONGLONG elapsed=mock.now-mock.started;return elapsed>=mock.budget ? 0 : mock.budget-(DWORD)elapsed;}
static void add_read(int count,int error,DWORD elapsed) {CHECK(mock.read_total<32);mock.reads[mock.read_total++]=(transfer_t){count,error,elapsed};}
static void add_send(int count,int error,DWORD elapsed) {CHECK(mock.send_total<32);mock.sends[mock.send_total++]=(transfer_t){count,error,elapsed};}
static void add_wait(DWORD result,DWORD elapsed) {CHECK(mock.wait_total<32);mock.waits[mock.wait_total++]=(wait_t){result,elapsed};}
static void add_network(int result,int error,long mask,int read_error,int close_error) {CHECK(mock.network_total<32);mock.network[mock.network_total++]=(network_t){result,error,mask,read_error,close_error};}
static void add_select(int result,int error,DWORD elapsed,bool writable) {CHECK(mock.select_total<32);mock.selects[mock.select_total++]=(select_t){result,error,elapsed,writable};}
static int WSAAPI fake_WSAGetLastError(void) {return mock.last_error;}
static ULONGLONG WINAPI fake_GetTickCount64(void) {return mock.now;}
static WSAEVENT WSAAPI fake_WSACreateEvent(void) {
 CHECK(!mock.event_open);++mock.creates;
 if(mock.create_error){mock.last_error=mock.create_error;return WSA_INVALID_EVENT;}
 mock.event_open=true;return NETEVENT;
}
static int WSAAPI fake_WSAEventSelect(SOCKET socket_value,WSAEVENT event,long mask) {
 CHECK(socket_value==SOCK && event==NETEVENT && mock.event_open);
 CHECK(mask==(FD_READ|FD_CLOSE));++mock.event_selects;
 if(mock.event_select_error){mock.last_error=mock.event_select_error;return SOCKET_ERROR;}return 0;
}
static BOOL WSAAPI fake_WSACloseEvent(WSAEVENT event) {
 CHECK(event==NETEVENT && mock.event_open);++mock.event_closes;mock.event_open=false;
 mock.last_error=WSAEINVAL;return TRUE;
}
static int WSAAPI fake_recv(SOCKET socket_value,char *output,int length,int flags) {
 CHECK(socket_value==SOCK && output && length>0 && flags==0);CHECK(mock.read_index<mock.read_total);
 transfer_t item=mock.reads[mock.read_index++];mock.last_recv_length=length;mock.now+=item.elapsed;
 if(item.count==SOCKET_ERROR){mock.last_error=item.error;return SOCKET_ERROR;}
 CHECK(item.count>=0 && item.count<=length);CHECK(mock.input_position+(unsigned)item.count<=sizeof(mock.input));
 memcpy(output,mock.input+mock.input_position,(size_t)item.count);mock.input_position+=(unsigned)item.count;return item.count;
}
static DWORD WINAPI fake_WaitForMultipleObjects(DWORD count,const HANDLE *events,BOOL all,DWORD timeout) {
 CHECK(count==2 && events && events[0]==NETEVENT && events[1]==WAKE && all==FALSE);
 CHECK(timeout>0 && timeout==remaining());CHECK(mock.wait_index<mock.wait_total);
 wait_t item=mock.waits[mock.wait_index++];CHECK(item.elapsed<=timeout);mock.now+=item.elapsed;
 if(item.result==WAIT_OBJECT_0)mock.network_signalled=true;
 if(item.result==WAIT_OBJECT_0+1)mock.wake_signalled=true;
 return item.result;
}
static BOOL WINAPI fake_ResetEvent(HANDLE event) {
 CHECK(event==WAKE && mock.wake_signalled);++mock.resets;
 if(!mock.reset_ok)return FALSE;
 mock.wake_signalled=false;return TRUE;
}
static int WSAAPI fake_WSAEnumNetworkEvents(SOCKET socket_value,WSAEVENT event,LPWSANETWORKEVENTS value) {
 CHECK(socket_value==SOCK && event==NETEVENT && value && mock.network_signalled);
 CHECK(mock.network_index<mock.network_total);network_t item=mock.network[mock.network_index++];
 if(item.result==SOCKET_ERROR){mock.last_error=item.error;return SOCKET_ERROR;}
 memset(value,0,sizeof(*value));value->lNetworkEvents=item.mask;
 value->iErrorCode[FD_READ_BIT]=item.read_error;value->iErrorCode[FD_CLOSE_BIT]=item.close_error;
 mock.network_signalled=false;return 0;
}
static int WSAAPI fake_send(SOCKET socket_value,const char *data,int length,int flags) {
 CHECK(socket_value==SOCK && data && length>0 && flags==0);CHECK(mock.send_index<mock.send_total);
 CHECK((const uint8_t *)data==mock.send_base+mock.sent_count);
 uint32_t left=mock.send_length-mock.sent_count;
 CHECK(length==(left>(uint32_t)INT_MAX ? INT_MAX : (int)left));
 transfer_t item=mock.sends[mock.send_index++];mock.last_send_length=length;mock.now+=item.elapsed;
 if(item.count==SOCKET_ERROR){mock.last_error=item.error;return SOCKET_ERROR;}
 CHECK(item.count>=0 && item.count<=length);CHECK(mock.sent_count+(unsigned)item.count<=sizeof(mock.sent));
 memcpy(mock.sent+mock.sent_count,data,(size_t)item.count);mock.sent_count+=(unsigned)item.count;return item.count;
}
static bool contains(const fd_set *set,SOCKET value) {
 if(!set)return false;
 for(u_int i=0;i<set->fd_count;++i)if(set->fd_array[i]==value)return true;
 return false;
}
static int WSAAPI fake_select(int nfds,fd_set *read,fd_set *write,fd_set *exception,const struct timeval *timeout) {
 CHECK(nfds==0 && !read && write && !exception && timeout && contains(write,SOCK));
 CHECK(timeout->tv_sec==(long)(remaining()/1000));CHECK(timeout->tv_usec==(long)((remaining()%1000)*1000));
 CHECK(mock.select_index<mock.select_total);select_t item=mock.selects[mock.select_index++];
 CHECK(item.elapsed<=remaining());mock.now+=item.elapsed;
 if(item.result==SOCKET_ERROR){mock.last_error=item.error;return SOCKET_ERROR;}
 FD_ZERO(write);if(item.result>0 && item.writable)FD_SET(SOCK,write);return item.result;
}
static int WSAAPI fake_FDIsSet(SOCKET value,fd_set *set) {CHECK(value==SOCK && set);return contains(set,value)?1:0;}
#define WSAGetLastError fake_WSAGetLastError
#define GetTickCount64 fake_GetTickCount64
#define WSACreateEvent fake_WSACreateEvent
#define WSAEventSelect fake_WSAEventSelect
#define WSACloseEvent fake_WSACloseEvent
#define recv fake_recv
#define WaitForMultipleObjects fake_WaitForMultipleObjects
#define ResetEvent fake_ResetEvent
#define WSAEnumNetworkEvents fake_WSAEnumNetworkEvents
#define send fake_send
#define select fake_select
#undef FD_ISSET
#define FD_ISSET(value,set) fake_FDIsSet((value),(set))
#include "uart_win32_bluetooth.h"

static void test_prepare(void) {
 WSAEVENT event=NETEVENT;
 reset();CHECK(pm3_bth_prepare_io(INVALID_SOCKET,&event)==WSAEINVAL);CHECK(event==WSA_INVALID_EVENT && mock.creates==0);
 reset();CHECK(pm3_bth_prepare_io(SOCK,NULL)==WSAEINVAL);CHECK(mock.creates==0);
 reset();mock.create_error=WSAENOBUFS;CHECK(pm3_bth_prepare_io(SOCK,&event)==WSAENOBUFS);CHECK(event==WSA_INVALID_EVENT && mock.creates==1 && mock.event_selects==0 && mock.event_closes==0);
 reset();mock.event_select_error=WSAENETDOWN;CHECK(pm3_bth_prepare_io(SOCK,&event)==WSAENETDOWN);CHECK(event==WSA_INVALID_EVENT && mock.creates==1 && mock.event_selects==1 && mock.event_closes==1 && !mock.event_open);
 reset();CHECK(pm3_bth_prepare_io(SOCK,&event)==0);CHECK(event==NETEVENT && mock.event_open && mock.event_closes==0);CHECK(fake_WSACloseEvent(event));
}
static void test_short_and_split_reads(void) {
 uint8_t output[34];uint32_t count;
 reset();memset(output,0xa5,sizeof(output));add_read(4,0,0);
 CHECK(pm3_bth_read_some(SOCK,NETEVENT,WAKE,output+1,32,&count,50)==PM3_BTH_IO_DATA);
 CHECK(count==4 && mock.read_index==1 && mock.wait_index==0);CHECK(!memcmp(output+1,mock.input,4));
 CHECK(output[0]==0xa5 && output[5]==0xa5 && output[33]==0xa5);
 reset();memset(output,0xa5,sizeof(output));unsigned position=0;const int sizes[]={1,2,5,8};
 for(unsigned i=0;i<4;++i){add_read(sizes[i],0,0);CHECK(pm3_bth_read_some(SOCK,NETEVENT,WAKE,output+position,16-position,&count,50)==PM3_BTH_IO_DATA);CHECK(count==(uint32_t)sizes[i]);position+=count;}
 CHECK(position==16 && !memcmp(output,mock.input,16));CHECK(mock.read_index==4 && mock.wait_index==0 && output[16]==0xa5);
 reset();add_read(2,0,0);CHECK(pm3_bth_read_some(SOCK,NETEVENT,WAKE,output,UINT32_MAX,&count,50)==PM3_BTH_IO_DATA);CHECK(count==2 && mock.last_recv_length==INT_MAX);
}
static void test_invalid_reads(void) {
 uint8_t output[8];uint32_t count=99;
 reset();CHECK(pm3_bth_read_some(INVALID_SOCKET,NETEVENT,WAKE,output,8,&count,50)==PM3_BTH_IO_ERROR && count==0);
 count=99;CHECK(pm3_bth_read_some(SOCK,WSA_INVALID_EVENT,WAKE,output,8,&count,50)==PM3_BTH_IO_ERROR && count==0);
 count=99;CHECK(pm3_bth_read_some(SOCK,NETEVENT,NULL,output,8,&count,50)==PM3_BTH_IO_ERROR && count==0);
 count=99;CHECK(pm3_bth_read_some(SOCK,NETEVENT,WAKE,NULL,8,&count,50)==PM3_BTH_IO_ERROR && count==0);
 count=99;CHECK(pm3_bth_read_some(SOCK,NETEVENT,WAKE,output,0,&count,50)==PM3_BTH_IO_ERROR && count==0);
 count=99;CHECK(pm3_bth_read_some(SOCK,NETEVENT,WAKE,output,8,&count,0)==PM3_BTH_IO_ERROR && count==0);
 CHECK(pm3_bth_read_some(SOCK,NETEVENT,WAKE,output,8,NULL,50)==PM3_BTH_IO_ERROR);
 CHECK(mock.read_index==0 && mock.wait_index==0 && mock.network_index==0);
}
static void test_wait_close_and_wake(void) {
 uint8_t output[16];uint32_t count;
 reset();add_read(SOCKET_ERROR,WSAEWOULDBLOCK,0);add_wait(WAIT_OBJECT_0,3);add_network(0,0,FD_READ,0,0);add_read(7,0,0);
 CHECK(pm3_bth_read_some(SOCK,NETEVENT,WAKE,output,16,&count,50)==PM3_BTH_IO_DATA && count==7);CHECK(mock.wait_index==1 && mock.network_index==1 && !memcmp(output,mock.input,7));
 reset();add_read(SOCKET_ERROR,WSAEWOULDBLOCK,0);add_wait(WAIT_TIMEOUT,50);
 CHECK(pm3_bth_read_some(SOCK,NETEVENT,WAKE,output,16,&count,50)==PM3_BTH_IO_EMPTY && count==0);CHECK(mock.now==1050);
 reset();add_read(0,0,0);CHECK(pm3_bth_read_some(SOCK,NETEVENT,WAKE,output,16,&count,50)==PM3_BTH_IO_CLOSED && count==0 && mock.wait_index==0);
 reset();add_read(5,0,0);add_read(SOCKET_ERROR,WSAEWOULDBLOCK,0);add_wait(WAIT_OBJECT_0,1);add_network(0,0,FD_CLOSE,0,0);add_read(0,0,0);
 CHECK(pm3_bth_read_some(SOCK,NETEVENT,WAKE,output,16,&count,50)==PM3_BTH_IO_DATA && count==5);
 mock.started=mock.now;CHECK(pm3_bth_read_some(SOCK,NETEVENT,WAKE,output,16,&count,50)==PM3_BTH_IO_CLOSED && count==0);
 reset();add_read(SOCKET_ERROR,WSAEWOULDBLOCK,0);add_wait(WAIT_OBJECT_0,1);add_network(0,0,FD_CLOSE,0,0);add_read(SOCKET_ERROR,WSAEWOULDBLOCK,0);
 CHECK(pm3_bth_read_some(SOCK,NETEVENT,WAKE,output,16,&count,50)==PM3_BTH_IO_CLOSED && count==0);
 CHECK(mock.wait_index==1 && mock.network_index==1 && mock.read_index==2 && mock.now==1001);
 reset();add_read(SOCKET_ERROR,WSAEWOULDBLOCK,0);add_wait(WAIT_OBJECT_0,1);add_network(0,0,FD_READ|FD_CLOSE,0,0);add_read(5,0,0);add_read(0,0,0);
 CHECK(pm3_bth_read_some(SOCK,NETEVENT,WAKE,output,16,&count,50)==PM3_BTH_IO_DATA && count==5 && !memcmp(output,mock.input,5));
 mock.started=mock.now;CHECK(pm3_bth_read_some(SOCK,NETEVENT,WAKE,output,16,&count,50)==PM3_BTH_IO_CLOSED && count==0 && mock.wait_index==1);
 reset();add_read(SOCKET_ERROR,WSAEWOULDBLOCK,0);add_wait(WAIT_OBJECT_0+1,1);
 CHECK(pm3_bth_read_some(SOCK,NETEVENT,WAKE,output,16,&count,50)==PM3_BTH_IO_WAKE && count==0);CHECK(mock.resets==1 && !mock.wake_signalled && mock.network_index==0);
 reset();mock.wake_signalled=true;add_read(4,0,0);add_read(SOCKET_ERROR,WSAEWOULDBLOCK,0);add_wait(WAIT_OBJECT_0+1,0);
 CHECK(pm3_bth_read_some(SOCK,NETEVENT,WAKE,output,16,&count,50)==PM3_BTH_IO_DATA && count==4 && mock.resets==0 && mock.wake_signalled);
 CHECK(pm3_bth_read_some(SOCK,NETEVENT,WAKE,output,16,&count,50)==PM3_BTH_IO_WAKE && count==0 && mock.resets==1 && !mock.wake_signalled);
 reset();add_read(SOCKET_ERROR,WSAEWOULDBLOCK,0);add_wait(WAIT_OBJECT_0,15);add_network(0,0,0,0,0);add_read(SOCKET_ERROR,WSAEWOULDBLOCK,0);add_wait(WAIT_OBJECT_0,20);add_network(0,0,FD_READ,0,0);add_read(SOCKET_ERROR,WSAEWOULDBLOCK,0);add_wait(WAIT_TIMEOUT,15);
 CHECK(pm3_bth_read_some(SOCK,NETEVENT,WAKE,output,16,&count,50)==PM3_BTH_IO_EMPTY && count==0);CHECK(mock.now==1050 && mock.read_index==3 && mock.wait_index==3 && mock.network_index==2);
 reset();add_read(SOCKET_ERROR,WSAEWOULDBLOCK,50);CHECK(pm3_bth_read_some(SOCK,NETEVENT,WAKE,output,16,&count,50)==PM3_BTH_IO_EMPTY && count==0 && mock.wait_index==0);
}
static void test_read_errors(void) {
 uint8_t output[16];uint32_t count;
 const int errors[]={WSAECONNRESET,WSAENETDOWN,0};
 for(unsigned i=0;i<3;++i){reset();add_read(SOCKET_ERROR,errors[i],0);CHECK(pm3_bth_read_some(SOCK,NETEVENT,WAKE,output,16,&count,50)==PM3_BTH_IO_ERROR && count==0 && mock.wait_index==0);}
 const DWORD waits[]={WAIT_FAILED,WAIT_ABANDONED_0};
 for(unsigned i=0;i<2;++i){reset();add_read(SOCKET_ERROR,WSAEWOULDBLOCK,0);add_wait(waits[i],1);CHECK(pm3_bth_read_some(SOCK,NETEVENT,WAKE,output,16,&count,50)==PM3_BTH_IO_ERROR && count==0 && mock.network_index==0);}
 reset();add_read(SOCKET_ERROR,WSAEWOULDBLOCK,0);add_wait(WAIT_OBJECT_0,1);add_network(SOCKET_ERROR,WSAEINVAL,0,0,0);
 CHECK(pm3_bth_read_some(SOCK,NETEVENT,WAKE,output,16,&count,50)==PM3_BTH_IO_ERROR && count==0);
 reset();add_read(SOCKET_ERROR,WSAEWOULDBLOCK,0);add_wait(WAIT_OBJECT_0,1);add_network(0,0,FD_READ,WSAECONNRESET,0);
 CHECK(pm3_bth_read_some(SOCK,NETEVENT,WAKE,output,16,&count,50)==PM3_BTH_IO_ERROR && count==0 && mock.read_index==1);
 reset();add_read(SOCKET_ERROR,WSAEWOULDBLOCK,0);add_wait(WAIT_OBJECT_0,1);add_network(0,0,FD_CLOSE,0,WSAECONNABORTED);
 CHECK(pm3_bth_read_some(SOCK,NETEVENT,WAKE,output,16,&count,50)==PM3_BTH_IO_ERROR && count==0 && mock.read_index==1);
 reset();mock.reset_ok=false;add_read(SOCKET_ERROR,WSAEWOULDBLOCK,0);add_wait(WAIT_OBJECT_0+1,1);
 CHECK(pm3_bth_read_some(SOCK,NETEVENT,WAKE,output,16,&count,50)==PM3_BTH_IO_ERROR && count==0 && mock.resets==1);
}
static int send_fixture(uint32_t length,DWORD budget) {mock.send_base=mock.input;mock.send_length=length;return pm3_bth_send_exact(SOCK,mock.input,length,budget);}
static void test_sends(void) {
 reset();add_send(16,0,0);CHECK(send_fixture(16,50)==0 && mock.sent_count==16 && !memcmp(mock.sent,mock.input,16));
 reset();add_send(3,0,1);add_send(5,0,1);add_send(8,0,1);CHECK(send_fixture(16,50)==0 && mock.send_index==3 && mock.sent_count==16 && !memcmp(mock.sent,mock.input,16));
 reset();add_send(4,0,1);add_send(SOCKET_ERROR,WSAEWOULDBLOCK,0);add_select(1,0,10,true);add_send(12,0,1);CHECK(send_fixture(16,50)==0 && mock.sent_count==16 && mock.select_index==1);
 reset();add_send(3,0,20);add_send(SOCKET_ERROR,WSAEWOULDBLOCK,0);add_select(1,0,10,true);add_send(4,0,15);add_send(SOCKET_ERROR,WSAEWOULDBLOCK,0);add_select(0,0,5,false);
 CHECK(send_fixture(16,50)==WSAETIMEDOUT && mock.sent_count==7 && mock.now==1050 && mock.send_index==4 && mock.select_index==2);
 reset();add_send(3,0,50);CHECK(send_fixture(16,50)==WSAETIMEDOUT && mock.sent_count==3 && mock.send_index==1);
 reset();add_send(SOCKET_ERROR,WSAEWOULDBLOCK,50);CHECK(send_fixture(16,50)==WSAETIMEDOUT && mock.select_index==0);
 reset();add_send(0,0,0);CHECK(send_fixture(16,50)==WSAECONNRESET && mock.sent_count==0);
 reset();add_send(3,0,0);add_send(0,0,0);CHECK(send_fixture(16,50)==WSAECONNRESET && mock.sent_count==3);
 const int errors[]={WSAECONNRESET,WSAEACCES,0};
 for(unsigned i=0;i<3;++i){reset();add_send(3,0,0);add_send(SOCKET_ERROR,errors[i],0);CHECK(send_fixture(16,50)==(errors[i]?errors[i]:WSAEINVAL) && mock.sent_count==3 && mock.send_index==2 && mock.select_index==0);}
 reset();add_send(SOCKET_ERROR,WSAEWOULDBLOCK,0);add_select(0,0,50,false);CHECK(send_fixture(16,50)==WSAETIMEDOUT);
 reset();add_send(SOCKET_ERROR,WSAEWOULDBLOCK,0);add_select(SOCKET_ERROR,WSAEINTR,1,false);CHECK(send_fixture(16,50)==WSAEINTR);
 reset();add_send(SOCKET_ERROR,WSAEWOULDBLOCK,0);add_select(SOCKET_ERROR,0,1,false);CHECK(send_fixture(16,50)==WSAEINVAL);
 reset();add_send(SOCKET_ERROR,WSAEWOULDBLOCK,0);add_select(1,0,1,false);CHECK(send_fixture(16,50)==WSAECONNABORTED);
 reset();add_send(SOCKET_ERROR,WSAECONNRESET,0);CHECK(send_fixture(UINT32_MAX,50)==WSAECONNRESET && mock.last_send_length==INT_MAX);
 reset();CHECK(pm3_bth_send_exact(INVALID_SOCKET,mock.input,16,50)==WSAEINVAL);CHECK(pm3_bth_send_exact(SOCK,NULL,16,50)==WSAEINVAL);CHECK(pm3_bth_send_exact(SOCK,mock.input,0,50)==WSAEINVAL);CHECK(pm3_bth_send_exact(SOCK,mock.input,16,0)==WSAEINVAL);CHECK(mock.send_index==0 && mock.select_index==0);
}
int main(void) {
 test_prepare();test_short_and_split_reads();test_invalid_reads();test_wait_close_and_wake();test_read_errors();test_sends();
 CHECK(!mock.event_open);
 printf("bluetooth IO helper: %u scenarios, %u assertions passed; hardware API calls mocked\n",scenarios,checks);return 0;
}
