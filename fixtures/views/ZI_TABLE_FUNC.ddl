// R-09 — an AMDP table function. Opaque to static analysis and to SAP's own
// dependency metadata (Appendix D.3). Read by ZI_USES_TABLE_FUNC.
define table function ZI_TABLE_FUNC
  returns {
    client   : abap.clnt;
    orderid  : abap.char(12);
    amount   : abap.curr(15,2);
  }
  implemented by method zcl_order_amdp=>calculate;
