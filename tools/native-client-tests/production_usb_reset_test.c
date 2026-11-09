/* Execute the actual production bulk decoder/engine with deterministic I/O.
 * No serial port, RFID device, copied download algorithm or firmware writes.
 */
/* Generated production bodies are inserted by run_usb_reset_tests.py. */

#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "usb_read_ng.h"
#define AT91C_EP_CONTROL 0
#define AT91C_EP_OUT 1
#define AT91C_EP_IN 2
#define AT91C_EP_NOTIFY 3
#define AT91C_UDP_RX_DATA_BK0 1u
#define AT91C_UDP_RX_DATA_BK1 2u
#define AT91C_UDP_ENDBUSRES 4u
#define AT91C_UDP_EPINT0 8u
#define AT91C_UDP_FEN 16u
#define AT91C_UDP_EPEDS 32u
#define AT91C_UDP_EPTYPE_CTRL 64u
#define AT91C_UDP_EPTYPE_BULK_OUT 128u
#define AT91C_UDP_EPTYPE_BULK_IN 256u
#define AT91C_UDP_EPTYPE_INT_IN 512u
#define AT91C_UDP_CONFG 1024u
#define AT91C_UDP_FADDEN 2048u
#define GPIO_USB_PU 1u
typedef uint32_t AT91_REG;
typedef struct {uint32_t UDP_ISR,UDP_ICR,UDP_RSTEP,UDP_FADDR,UDP_GLBSTATE,UDP_CSR[4];} udp_t;
typedef struct {uint32_t PIO_ODR;} pio_t;
static udp_t registers;
static udp_t *pUdp=&registers;
static pio_t pio;
#define AT91C_BASE_PIOA (&pio)
static uint8_t btConfiguration,btConnection,btReceiveBank;
static unsigned zlp_calls,stall_calls,enum_calls,poll_calls,clear_calls;
static uint16_t available;
static bool poll_ready;
static void AT91F_USB_SendZlp(udp_t *udp){(void)udp;zlp_calls++;}
static void AT91F_USB_SendStall(udp_t *udp){(void)udp;stall_calls++;}
static void AT91F_CDC_Enumerate(void){enum_calls++;}
static bool usb_poll(void){poll_calls++;return poll_ready;}
static uint16_t usb_available_length(void){return available;}
static void usb_read_ng_clear(void){clear_calls++;}

/* @USB_RESET_PRODUCTION_BODIES@ */

static uint8_t cached[64],packets[32][64],packet_lengths[32],reset_payload[64];
static size_t packet_count,packet_index,packet_offset,link_calls,reset_at_link,reset_payload_len;
static bool link_ready=true;
static void feed(const uint8_t *bytes,size_t count){
 packet_count=packet_index=packet_offset=0;
 while(count){size_t n=count>64?64:count;assert(packet_count<32);memcpy(packets[packet_count],bytes,n);packet_lengths[packet_count++]=(uint8_t)n;bytes+=n;count-=n;}
}
static bool fixture_link(void){
 link_calls++;
 if(reset_at_link&&link_calls==reset_at_link){usb_read_ng_reset();feed(reset_payload,reset_payload_len);}
 return link_ready;
}
static bool fixture_data(void){return packet_index<packet_count;}
static uint16_t fixture_count(void){return packet_lengths[packet_index];}
static uint8_t fixture_read(void){assert(packet_index<packet_count);assert(packet_offset<packet_lengths[packet_index]);return packets[packet_index][packet_offset++];}
static void fixture_clear(void){assert(packet_offset==packet_lengths[packet_index]);packet_index++;packet_offset=0;}
static const usb_read_ng_config_t cfg={fixture_link,fixture_data,fixture_count,fixture_read,fixture_clear,cached,sizeof(cached),2};
static void reset(void){
 memset(&registers,0,sizeof(registers));memset(&pio,0,sizeof(pio));
 btConfiguration=1;btConnection=3;btReceiveBank=AT91C_UDP_RX_DATA_BK1;
 link_ready=true;packet_count=packet_index=packet_offset=link_calls=reset_at_link=reset_payload_len=0;
 zlp_calls=stall_calls=enum_calls=poll_calls=clear_calls=available=0;poll_ready=false;
 usb_read_ng_init(&cfg);
}
static void cache_old_command(void){uint8_t old[42],out[8];memset(old,0xA5,sizeof(old));feed(old,sizeof(old));assert(usb_read_ng(out,8)==8);assert(usb_read_ng_has_buffered_data());}
int main(void){
 unsigned cases=0;uint8_t input[1024],output[1024];for(size_t i=0;i<sizeof(input);i++)input[i]=(uint8_t)(i*13+7);
 usb_read_ng_reset();assert(!usb_read_ng_has_buffered_data());cases++;
 reset();cache_old_command();usb_read_ng_reset();assert(!usb_read_ng_has_buffered_data());feed(input,42);assert(usb_read_ng(output,42)==42);assert(!memcmp(input,output,42));cases++;
 reset();cache_old_command();usb_read_ng_reset();usb_read_ng_reset();assert(!usb_read_ng_has_buffered_data());feed(input,42);assert(usb_read_ng(output,42)==42);cases++;
 reset();cache_old_command();reset_at_link=link_calls+1;memcpy(reset_payload,input,42);reset_payload_len=42;assert(usb_read_ng(output,32)==0);assert(!usb_read_ng_has_buffered_data());assert(usb_read_ng(output,42)==42);assert(!memcmp(input,output,42));cases++;
 reset();cache_old_command();link_ready=false;assert(usb_read_ng(output,32)==0);assert(!usb_read_ng_has_buffered_data());link_ready=true;feed(input,42);assert(usb_read_ng(output,42)==42);assert(!memcmp(input,output,42));cases++;
 reset();feed(input,128);reset_at_link=3;memcpy(reset_payload,input+200,42);reset_payload_len=42;assert(usb_read_ng(output,100)==0);assert(usb_read_ng(output,42)==42);assert(!memcmp(input+200,output,42));cases++;
 reset();cache_old_command();usb_reset_rx_state();assert(btReceiveBank==AT91C_UDP_RX_DATA_BK0);assert(!usb_read_ng_has_buffered_data());cases++;
 reset();cache_old_command();usb_reset_endpoint(1u<<AT91C_EP_IN);assert(btReceiveBank==AT91C_UDP_RX_DATA_BK1);assert(usb_read_ng_has_buffered_data());assert(registers.UDP_RSTEP==0);cases++;
 reset();cache_old_command();usb_reset_endpoint(1u<<AT91C_EP_OUT);assert(btReceiveBank==AT91C_UDP_RX_DATA_BK0);assert(!usb_read_ng_has_buffered_data());cases++;
 reset();cache_old_command();registers.UDP_ISR=AT91C_UDP_ENDBUSRES;assert(!usb_check());assert(btConfiguration==0&&btConnection==0);assert(btReceiveBank==AT91C_UDP_RX_DATA_BK0);assert(!usb_read_ng_has_buffered_data());assert(registers.UDP_ICR==AT91C_UDP_ENDBUSRES);assert(registers.UDP_CSR[0]==(AT91C_UDP_EPEDS|AT91C_UDP_EPTYPE_CTRL));cases++;
 reset();cache_old_command();registers.UDP_ISR=AT91C_UDP_ENDBUSRES;assert(!usb_poll_validate_length());assert(!usb_read_ng_has_buffered_data());assert(poll_calls==0);cases++;
 reset();cache_old_command();assert(usb_poll_validate_length());assert(poll_calls==0);cases++;
 reset();poll_ready=true;available=0;assert(!usb_poll_validate_length());assert(clear_calls==1);cases++;
 reset();cache_old_command();configure_endpoint_fixture(1);assert(btConfiguration==1&&btConnection==0);assert(btReceiveBank==AT91C_UDP_RX_DATA_BK0);assert(!usb_read_ng_has_buffered_data());assert(registers.UDP_CSR[1]==(AT91C_UDP_EPEDS|AT91C_UDP_EPTYPE_BULK_OUT));assert(registers.UDP_CSR[2]==(AT91C_UDP_EPEDS|AT91C_UDP_EPTYPE_BULK_IN));assert(zlp_calls==1);cases++;
 reset();cache_old_command();configure_endpoint_fixture(0);assert(btConfiguration==0);assert(!usb_read_ng_has_buffered_data());for(size_t i=1;i<4;i++)assert(registers.UDP_CSR[i]==0);cases++;
 reset();cache_old_command();clear_endpoint_fixture(0,AT91C_EP_OUT);assert(btReceiveBank==AT91C_UDP_RX_DATA_BK0);assert(!usb_read_ng_has_buffered_data());assert(zlp_calls==1);cases++;
 reset();cache_old_command();clear_endpoint_fixture(0,0x80|AT91C_EP_IN);assert(usb_read_ng_has_buffered_data());assert(btReceiveBank==AT91C_UDP_RX_DATA_BK1);assert(registers.UDP_CSR[2]==(AT91C_UDP_EPEDS|AT91C_UDP_EPTYPE_BULK_IN));assert(zlp_calls==1);cases++;
 reset();cache_old_command();usb_disable();assert(btConfiguration==0&&btConnection==0);assert(btReceiveBank==AT91C_UDP_RX_DATA_BK0);assert(!usb_read_ng_has_buffered_data());assert(pio.PIO_ODR==GPIO_USB_PU);cases++;
 const size_t sizes[]={1,8,10,31,32,42,44,63,64,65,128,255,512,624,1024};
 for(size_t c=0;c<sizeof(sizes)/sizeof(sizes[0]);c++){
  reset();feed(input,sizes[c]);size_t pos=0;
  while(pos<sizes[c]){size_t n=(pos%19)+1;if(n>sizes[c]-pos)n=sizes[c]-pos;assert(usb_read_ng(output+pos,n)==n);pos+=n;}
  assert(!memcmp(input,output,sizes[c]));assert(!usb_read_ng_has_buffered_data());cases++;
 }
 printf("%u production USB reset/RX cases passed; FIFO/register callbacks only; no hardware\n",cases);
 return 0;
}
