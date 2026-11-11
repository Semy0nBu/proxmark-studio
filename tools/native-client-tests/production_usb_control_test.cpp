//-----------------------------------------------------------------------------
// Copyright (C) Proxmark3 contributors. See AUTHORS.md for details.
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// See LICENSE.txt for the text of the license.
//-----------------------------------------------------------------------------
// Register/FIFO fixture. Actual firmware control-transfer bodies are inserted.
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <deque>
#include <vector>

#define AT91C_UDP_TXCOMP 1u
#define AT91C_UDP_RX_DATA_BK0 2u
#define AT91C_UDP_RXSETUP 4u
#define AT91C_UDP_STALLSENT 8u
#define AT91C_UDP_TXPKTRDY 16u
#define AT91C_UDP_FORCESTALL 32u
#define AT91C_UDP_RX_DATA_BK1 64u
#define AT91C_UDP_DIR 128u
#define AT91C_UDP_EPEDS 32768u
#define AT91C_UDP_EPTYPE_CTRL 0u
#define AT91C_UDP_EPTYPE_BULK_OUT 512u
#define AT91C_UDP_EPTYPE_BULK_IN 1536u
#define AT91C_UDP_EPTYPE_INT_IN 1792u
#define AT91C_UDP_RXBYTECNT (2047u << 16)
#define AT91C_UDP_ENDBUSRES 4096u
#define AT91C_UDP_EPINT0 1u
#define AT91C_UDP_FEN 256u
#define AT91C_UDP_FADDEN 1u
#define AT91C_UDP_CONFG 2u
#define USART_BAUD_RATE 115200u
#define PACKED __attribute__((packed))
#define MIN(a,b) ((a)<(b)?(a):(b))
#define PM3_SUCCESS 0
#define PM3_EINVARG -2
#define PM3_EIO -4
#define USB_CDC_DESC_MS_VENDOR_CODE 0x1c
#define MS_OS_DESCRIPTOR_INDEX 0xee
#define MS_EXTENDED_COMPAT_ID 4
#define MS_EXTENDED_PROPERTIES 5
#define MS_WCID_GET_DESCRIPTOR 0xc0
#define MS_WCID_GET_FEATURE_DESCRIPTOR 0xc1
using AT91_REG = uint32_t;
enum {PLAIN, ISR, ICR, RSTEP, CSR, FDR};
struct Register {
    int kind = PLAIN, endpoint = 0;
    uint32_t value = 0;
    Register() = default;
    Register(int k,int e=0):kind(k),endpoint(e){}
    operator uint32_t() const;
    Register &operator=(uint32_t v);
};
struct UDP {
    Register UDP_ISR{ISR}, UDP_ICR{ICR}, UDP_RSTEP{RSTEP}, UDP_FADDR, UDP_GLBSTATE;
    Register UDP_CSR[4]{{CSR,0},{CSR,1},{CSR,2},{CSR,3}};
    Register UDP_FDR[4]{{FDR,0},{FDR,1},{FDR,2},{FDR,3}};
};
using AT91PS_UDP = UDP *;
static UDP registers;
static AT91PS_UDP pUdp = &registers;
static uint8_t btConfiguration, btConnection, btReceiveBank;
static uint32_t reads, ticks, watchdog, cache_resets, endpoint_resets[4], read_bytes[4];
static uint32_t ack_delay, reset_at, setup_at, payload_at, status_out_at;
static uint32_t queued[4], pending_ack[4], ack_limit, sticky_clear, delayed_dir;
static bool ack_enabled, stall_enabled, payload_enabled, setup_with_ack, setup_on_ack_clear, frozen_ticks;
static std::deque<uint8_t> rx[4];
static std::vector<uint8_t> tx[4], payload;
static std::vector<std::vector<uint8_t>> packets[4];
static void host_setup(uint8_t type,uint8_t request,uint16_t value=0,uint16_t index=0,uint16_t length=0);
static void hardware_step() {
    ++reads;
    if (reset_at && reads >= reset_at) {
        reset_at = 0;registers.UDP_ISR.value |= AT91C_UDP_ENDBUSRES;
    }
    if (setup_at && reads >= setup_at) {
        setup_at = 0;host_setup(0x80,6,0x100,0,18);
    }
    if (payload_at && reads >= payload_at) {
        payload_at = 0;rx[0] = std::deque<uint8_t>(payload.begin(),payload.end());
        registers.UDP_CSR[0].value = (registers.UDP_CSR[0].value & ~AT91C_UDP_RXBYTECNT) |
            AT91C_UDP_RX_DATA_BK0 | (static_cast<uint32_t>(rx[0].size()) << 16);
    }
    if (status_out_at && reads >= status_out_at) {
        status_out_at = 0;registers.UDP_CSR[0].value |= AT91C_UDP_RX_DATA_BK0;
    }
    for (int ep=0;ep<4;ep++) {
        if (pending_ack[ep] && reads >= pending_ack[ep] && ack_enabled && queued[ep] <= ack_limit) {
            pending_ack[ep] = 0;
            registers.UDP_CSR[ep].value &= ~AT91C_UDP_TXPKTRDY;
            registers.UDP_CSR[ep].value |= AT91C_UDP_TXCOMP;
            if (ep==0&&setup_with_ack) {
                setup_with_ack=false;host_setup(0x80,6,0x100,0,18);
                registers.UDP_CSR[0].value |= AT91C_UDP_TXCOMP;
            }
        }
    }
    if (delayed_dir && --delayed_dir == 0)registers.UDP_CSR[0].value |= AT91C_UDP_DIR;
}
Register::operator uint32_t() const {
    hardware_step();
    if (kind == FDR) {
        assert(!rx[endpoint].empty());
        uint32_t byte = rx[endpoint].front();rx[endpoint].pop_front();++read_bytes[endpoint];return byte;
    }
    return value;
}
Register &Register::operator=(uint32_t v) {
    hardware_step();
    if (kind == FDR) {tx[endpoint].push_back(static_cast<uint8_t>(v));return *this;}
    if (kind == ICR) {registers.UDP_ISR.value &= ~v;value = v;return *this;}
    if (kind == RSTEP && v) {
        for (int ep=0;ep<4;ep++)if (v & (1u<<ep)) {
            ++endpoint_resets[ep];registers.UDP_CSR[ep].value = 0;rx[ep].clear();tx[ep].clear();pending_ack[ep] = 0;
        }
    }
    if (kind != CSR) {value = v;return *this;}
    uint32_t old = value;
    const uint32_t status = AT91C_UDP_TXCOMP | AT91C_UDP_RX_DATA_BK0 | AT91C_UDP_RX_DATA_BK1 |
        AT91C_UDP_RXSETUP | AT91C_UDP_STALLSENT;
    value = (old & status & (v | sticky_clear)) | (v & ~(status | AT91C_UDP_RXBYTECNT)) | (old & AT91C_UDP_RXBYTECNT);
    if ((old&AT91C_UDP_TXCOMP)&&!(v&AT91C_UDP_TXCOMP)&&endpoint==0&&setup_on_ack_clear) {
        setup_on_ack_clear=false;host_setup(0x80,6,0x100,0,18);return *this;
    }
    if ((old & AT91C_UDP_RX_DATA_BK0) && !(value & AT91C_UDP_RX_DATA_BK0)) {
        rx[endpoint].clear();value &= ~AT91C_UDP_RXBYTECNT;
    }
    if ((old & AT91C_UDP_RXSETUP) && !(value & AT91C_UDP_RXSETUP) && payload_enabled)payload_at = reads + 5;
    if (!(old & AT91C_UDP_DIR) && (v & AT91C_UDP_DIR) && delayed_dir)value &= ~AT91C_UDP_DIR;
    if ((value & AT91C_UDP_FORCESTALL) && stall_enabled)value |= AT91C_UDP_STALLSENT;
    if (!(old & AT91C_UDP_TXPKTRDY) && (value & AT91C_UDP_TXPKTRDY)) {
        packets[endpoint].push_back(tx[endpoint]);tx[endpoint].clear();++queued[endpoint];pending_ack[endpoint] = reads + ack_delay;
    }
    return *this;
}
#ifndef AS_BOOTROM
static uint32_t GetTickCount() {return frozen_ticks?ticks:ticks++;}
#endif
#define WDT_HIT() (++watchdog)
#ifndef AS_BOOTROM
static void usb_read_ng_reset() {++cache_resets;}
#define EXPECTED_CACHE_RESET 1
#else
#define EXPECTED_CACHE_RESET 0
#endif
bool usb_check(void);
bool AT91F_USB_SendData(AT91PS_UDP,const char *,uint32_t,uint32_t);
bool AT91F_USB_SendZlp(AT91PS_UDP);
void AT91F_USB_SendStall(AT91PS_UDP);
void AT91F_CDC_Enumerate(void);
static const char devDescriptor[18] = {18,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17};
static const char cfgDescriptor[67] = {9,2};
static const char bosDescriptor[12] = {5,15};
static const char StrLanguageCodes[4] = {4,3};
static const char StrManufacturer[26] = {26,3};
static const char StrProduct[20] = {20,3};
static const char StrSerialNumber[14] = {14,3};
static const char StrMS_OSDescriptor[18] = {18,3};
static const char CompatIDFeatureDescriptor[40] = {40};
static const char OSprop[304] = {48,1};

/* @USB_CONTROL_PRODUCTION_BODIES@ */

static void reset_fixture() {
    registers = UDP();btConfiguration = 1;btConnection = 3;btReceiveBank = AT91C_UDP_RX_DATA_BK1;
    reads = ticks = watchdog = cache_resets = reset_at = setup_at = payload_at = status_out_at = sticky_clear = delayed_dir = 0;
    ack_delay = 5;ack_limit = UINT32_MAX;ack_enabled = stall_enabled = true;
    payload_enabled = setup_with_ack = setup_on_ack_clear = frozen_ticks = false;
    payload.assign(7,0x12);
    for (int ep=0;ep<4;ep++) {
        rx[ep].clear();tx[ep].clear();packets[ep].clear();endpoint_resets[ep] = queued[ep] = pending_ack[ep] = read_bytes[ep] = 0;
        registers.UDP_CSR[ep].value = AT91C_UDP_EPEDS;
    }
}
static void host_setup(uint8_t type,uint8_t request,uint16_t value,uint16_t index,uint16_t length) {
    rx[0] = {type,request,static_cast<uint8_t>(value),static_cast<uint8_t>(value>>8),
        static_cast<uint8_t>(index),static_cast<uint8_t>(index>>8),static_cast<uint8_t>(length),static_cast<uint8_t>(length>>8)};
    registers.UDP_CSR[0].value &= ~(AT91C_UDP_TXPKTRDY | AT91C_UDP_TXCOMP | AT91C_UDP_RX_DATA_BK0 | AT91C_UDP_RXBYTECNT);
    registers.UDP_CSR[0].value |= AT91C_UDP_RXSETUP | (8u<<16);
    registers.UDP_ISR.value |= AT91C_UDP_EPINT0;pending_ack[0] = 0;tx[0].clear();
}
static void assert_pending_setup() {
    assert(registers.UDP_CSR[0].value & AT91C_UDP_RXSETUP);assert(rx[0].size()==8);assert(endpoint_resets[0]==0);
}
static void next_descriptor() {
    ack_enabled = true;payload_enabled = false;payload_at = 0;
    host_setup(0x80,6,0x100,0,18);AT91F_CDC_Enumerate();
    assert(!(registers.UDP_CSR[0].value & AT91C_UDP_RXSETUP));assert(packets[0].back().size()==2);
}
int main() {
    unsigned cases=0;char data[128];for (unsigned i=0;i<sizeof(data);i++)data[i]=static_cast<char>(i);
    reset_fixture();assert(AT91F_USB_SendZlp(pUdp));assert(packets[0].size()==1&&packets[0][0].empty());cases++;
    reset_fixture();ack_delay=200;assert(AT91F_USB_SendZlp(pUdp));assert(reads>=200);cases++;
    reset_fixture();ack_enabled=false;assert(!AT91F_USB_SendZlp(pUdp));assert(endpoint_resets[0]==1&&watchdog);next_descriptor();cases++;
    reset_fixture();ack_enabled=false;ticks=UINT32_MAX-100;assert(!AT91F_USB_SendZlp(pUdp));assert(endpoint_resets[0]==1);cases++;
    reset_fixture();ack_enabled=false;frozen_ticks=true;assert(!AT91F_USB_SendZlp(pUdp));assert(endpoint_resets[0]==1&&watchdog>=USB_TRANSFER_SPIN_LIMIT);cases++;
    reset_fixture();ack_enabled=false;registers.UDP_CSR[0].value|=AT91C_UDP_TXCOMP;assert(!AT91F_USB_SendZlp(pUdp));assert(endpoint_resets[0]==1);cases++;
    reset_fixture();ack_enabled=false;setup_at=20;assert(!AT91F_USB_SendZlp(pUdp));assert_pending_setup();next_descriptor();cases++;
    reset_fixture();ack_enabled=false;reset_at=20;assert(!AT91F_USB_SendZlp(pUdp));assert(registers.UDP_ISR.value&AT91C_UDP_ENDBUSRES);assert(!usb_check());assert(!btConfiguration&&!btConnection&&btReceiveBank==AT91C_UDP_RX_DATA_BK0);next_descriptor();cases++;
    const unsigned lengths[]={0,1,7,8,9,16,18,40,64,128};
    for (unsigned length:lengths) {
        reset_fixture();assert(AT91F_USB_SendData(pUdp,data,length,length));std::vector<uint8_t> bytes;
        for (const auto &packet:packets[0])bytes.insert(bytes.end(),packet.begin(),packet.end());
        assert(bytes.size()==length&&!std::memcmp(bytes.data(),data,length));cases++;
    }
    reset_fixture();assert(AT91F_USB_SendData(pUdp,data,16,32));assert(packets[0].size()==3&&packets[0].back().empty());cases++;
    reset_fixture();assert(AT91F_USB_SendData(pUdp,data,18,8));assert(packets[0].size()==1&&packets[0][0].size()==8);cases++;
    reset_fixture();ack_enabled=false;status_out_at=20;assert(AT91F_USB_SendData(pUdp,data,128,128));assert(packets[0].size()==1&&endpoint_resets[0]==1);cases++;
    reset_fixture();ack_enabled=false;assert(!AT91F_USB_SendData(pUdp,data,18,18));assert(endpoint_resets[0]==1);next_descriptor();cases++;
    reset_fixture();ack_enabled=false;setup_at=20;assert(!AT91F_USB_SendData(pUdp,data,18,18));assert_pending_setup();next_descriptor();cases++;
    reset_fixture();ack_enabled=false;reset_at=20;assert(!AT91F_USB_SendData(pUdp,data,18,18));assert(registers.UDP_ISR.value&AT91C_UDP_ENDBUSRES);cases++;
    reset_fixture();payload_enabled=true;host_setup(0x21,0x20,0,0,7);AT91F_CDC_Enumerate();assert(read_bytes[0]==15&&packets[0].size()==1&&packets[0][0].empty());cases++;
    reset_fixture();host_setup(0x21,0x20,0,0,6);AT91F_CDC_Enumerate();assert(packets[0].empty()&&read_bytes[0]==8);next_descriptor();cases++;
    reset_fixture();payload_enabled=true;payload.resize(6);host_setup(0x21,0x20,0,0,7);AT91F_CDC_Enumerate();assert(packets[0].empty());next_descriptor();cases++;
    reset_fixture();payload_enabled=true;payload.resize(8);host_setup(0x21,0x20,0,0,7);AT91F_CDC_Enumerate();assert(packets[0].empty());next_descriptor();cases++;
    reset_fixture();payload_enabled=true;ack_enabled=false;host_setup(0x21,0x20,0,0,7);AT91F_CDC_Enumerate();assert(read_bytes[0]==15&&endpoint_resets[0]==1);next_descriptor();cases++;
    reset_fixture();host_setup(0x21,0x20,0,0,7);AT91F_CDC_Enumerate();assert(endpoint_resets[0]==1&&packets[0].empty());next_descriptor();cases++;
    reset_fixture();host_setup(0x21,0x20,0,0,7);setup_at=35;AT91F_CDC_Enumerate();assert_pending_setup();next_descriptor();cases++;
    reset_fixture();host_setup(0x21,0x20,0,0,7);reset_at=35;AT91F_CDC_Enumerate();assert(registers.UDP_ISR.value&AT91C_UDP_ENDBUSRES);assert(!usb_check());next_descriptor();cases++;
    reset_fixture();host_setup(0x80,6,0x100,0,18);delayed_dir=20;AT91F_CDC_Enumerate();assert(packets[0].size()==3);cases++;
    for (uint8_t request:{uint8_t(5),uint8_t(9),uint8_t(0x22)}) {
        reset_fixture();btConfiguration=0;btConnection=0;host_setup(request==0x22?0x21:0,request,request==5?7:1);AT91F_CDC_Enumerate();
        if (request==5)assert((registers.UDP_FADDR.value&127)==7);
        if (request==9)assert(btConfiguration==1&&cache_resets==EXPECTED_CACHE_RESET);
        if (request==0x22)assert(btConnection==1);
        cases++;
        reset_fixture();btConfiguration=0;btConnection=0;ack_enabled=false;host_setup(request==0x22?0x21:0,request,request==5?7:1);AT91F_CDC_Enumerate();
        assert(!btConfiguration&&!btConnection&&!registers.UDP_FADDR.value);next_descriptor();cases++;
        reset_fixture();btConfiguration=0;btConnection=0;ack_enabled=false;host_setup(request==0x22?0x21:0,request,request==5?7:1);setup_at=40;AT91F_CDC_Enumerate();
        assert(!btConfiguration&&!btConnection&&!registers.UDP_FADDR.value);assert_pending_setup();next_descriptor();cases++;
        reset_fixture();btConfiguration=0;btConnection=0;setup_with_ack=true;host_setup(request==0x22?0x21:0,request,request==5?7:1);AT91F_CDC_Enumerate();
        assert(!btConfiguration&&!btConnection&&!registers.UDP_FADDR.value);assert_pending_setup();next_descriptor();cases++;
        reset_fixture();btConfiguration=0;btConnection=0;setup_on_ack_clear=true;host_setup(request==0x22?0x21:0,request,request==5?7:1);AT91F_CDC_Enumerate();
        assert(!btConfiguration&&!btConnection&&!registers.UDP_FADDR.value);assert_pending_setup();next_descriptor();cases++;
        reset_fixture();btConfiguration=0;btConnection=0;ack_enabled=false;host_setup(request==0x22?0x21:0,request,request==5?7:1);reset_at=40;AT91F_CDC_Enumerate();
        assert(!btConfiguration&&!btConnection&&!registers.UDP_FADDR.value);assert(registers.UDP_ISR.value&AT91C_UDP_ENDBUSRES);assert(!usb_check());next_descriptor();cases++;
    }
    reset_fixture();ack_enabled=false;host_setup(0,9,0);AT91F_CDC_Enumerate();assert(btConfiguration==1&&btConnection==3&&cache_resets==0);next_descriptor();cases++;
    const unsigned bulk_lengths[]={1,63,64,65,128};
    for (unsigned length:bulk_lengths) {
        reset_fixture();assert(usb_write(reinterpret_cast<const uint8_t *>(data),length)==PM3_SUCCESS);
        std::vector<uint8_t> bytes;for (const auto &packet:packets[2])bytes.insert(bytes.end(),packet.begin(),packet.end());
        assert(bytes.size()==length&&!std::memcmp(bytes.data(),data,length));cases++;
    }
    reset_fixture();ack_limit=1;assert(usb_write(reinterpret_cast<const uint8_t *>(data),64)==PM3_EIO);assert(packets[2].size()==2&&packets[2].back().empty());assert(endpoint_resets[2]==1&&btReceiveBank==AT91C_UDP_RX_DATA_BK1);cases++;
    reset_fixture();ack_enabled=false;reset_at=30;assert(usb_write(reinterpret_cast<const uint8_t *>(data),64)==PM3_EIO);assert(!usb_check());assert(!btConfiguration);cases++;
    reset_fixture();registers.UDP_CSR[0].value|=AT91C_UDP_TXCOMP;sticky_clear=AT91C_UDP_TXCOMP;assert(!AT91F_USB_SendData(pUdp,data,18,18));assert(endpoint_resets[0]==1);cases++;
    std::printf("%u production USB EP0/bulk cases passed; real control bodies, register/FIFO callbacks; no device I/O\n",cases);
    return 0;
}
