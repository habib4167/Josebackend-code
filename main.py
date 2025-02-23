from argparse import Action
from typing import Dict, List, Optional, Tuple, Union
from fastapi import FastAPI, HTTPException
from ib_insync import IB, util, Stock, Option, Order, LimitOrder
import asyncio
from datetime import datetime
from pydantic import BaseModel, Field, validator
from enum import Enum
from fastapi_utilities import repeat_every, repeat_at

# Initialize FastAPI app
app = FastAPI(title="IBKR API", description="Interactive Brokers API Integration")

# Initialize IB instance
ib = IB()

# Track connection status
is_connected = False
target_symbol = 'AAPL'
bot_running = False

class OptionType(str, Enum):
    CALL = "CALL"
    PUT = "PUT"

class OrderAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

class OrderType(str, Enum):
    MARKET = "MKT"
    LIMIT = "LMT"

class SignalType(str, Enum):
    RED = "RED"
    GREEN = "GREEN"

class OptionContractRequest(BaseModel):
    symbol: str = Field(..., description="The underlying stock symbol (e.g., 'AAPL')")
    option_type: OptionType = Field(..., description="Option type (CALL or PUT)")
    strike_price: float = Field(..., gt=0, description="Strike price of the option")
    expiration_date: str = Field(..., description="Option expiration date (YYYYMMDD format)")
    order_type: OrderType = Field(default=OrderType.MARKET, description="Order type (MKT or LMT)")
    limit_price: Optional[float] = Field(None, gt=0, description="Limit price (required for limit orders)")
    quantity: int = Field(..., gt=0, description="Number of contracts")
    exchange: str = Field(default="NASDAQBX", description="Exchange (default: NASDAQBX)")
    currency: str = Field(default="USD", description="Currency (default: USD)")

    @validator('expiration_date')
    def validate_expiration_date(cls, v):
        try:
            datetime.strptime(v, '%Y%m%d')
            return v
        except ValueError:
            raise ValueError('expiration_date must be in YYYYMMDD format')

    @validator('limit_price')
    def validate_limit_price(cls, v, values):
        if values.get('order_type') == OrderType.LIMIT and v is None:
            raise ValueError('limit_price is required for limit orders')
        return v

class ConnectionConfig(BaseModel):
    host: str = "127.0.0.1"  # TWS/Gateway hostname
    port: int = 7497         # TWS live (7496 for Gateway, 7497 for TWS)
    client_id: int = 1       # Unique client identifier

@app.on_event("shutdown")
async def shutdown_event():
    """Disconnect from IBKR when shutting down the application"""
    if ib.isConnected():
        ib.disconnect()

@app.post('/bot-start')
async def start_bot():
    global bot_running
    bot_running = True
    return {"status": "Bot started"}

@app.post('/bot-stop')
async def start_bot():
    global bot_running
    bot_running = False
    return {"status": "Bot stopped"}

@app.get("/target-symbol")
def get_target_symbol() -> str:
    return target_symbol


@app.post("/target-symbol")
def set_target_symbol(symbol: str):
    global target_symbol
    target_symbol = symbol

@app.get("/status")
async def get_connection_status() -> Dict[str, bool]:
    """Get current IBKR connection status"""
    return {"connected": ib.isConnected()}

@app.post("/connect")
async def connect_to_ibkr() :
    config = ConnectionConfig()
    try:
        # Disconnect if already connected
        if ib.isConnected():
            ib.disconnect()
        
        # Connect to TWS/Gateway
        await ib.connectAsync(
            host=config.host,
            port=config.port,
            clientId=config.client_id,
            readonly=False  # Set to False to enable order placement
        )

        if ib.isConnected():
            return True
        else:
            raise HTTPException(
                status_code=500,
                detail="Failed to establish connection with IBKR"
            )
            
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error connecting to IBKR: {str(e)}"
        )

@app.post("/disconnect")
async def disconnect_from_ibkr() -> Dict[str, str]:
    """Disconnect from IBKR TWS/Gateway"""
    if not ib.isConnected():
        raise HTTPException(
            status_code=400,
            detail="Not connected to IBKR"
        )
    
    try:
        ib.disconnect()
        return {"status": "Successfully disconnected from IBKR"}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error disconnecting from IBKR: {str(e)}"
        )

async def validate_option_contract(contract: Option) -> Option:
    """
    Validate and qualify an option contract
    """
    try:
        # First validate the underlying stock
        stock = Stock(contract.symbol, "SMART", contract.currency)
        qualified_stocks = await ib.qualifyContractsAsync(stock)
        
        if not qualified_stocks:
            raise HTTPException(
                status_code=404,
                detail=f"No valid contract found for symbol {contract.symbol}"
            )
        
        # Now validate the option contract
        details = await ib.reqContractDetailsAsync(contract)
        if not details:
            raise HTTPException(
                status_code=404,
                detail=f"No valid option contract found for {contract.symbol} with strike {contract.strike} and expiration {contract.lastTradeDateOrContractMonth}"
            )
        
        return details[0].contract
        
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Error validating option contract: {str(e)}"
        )

@app.get("/account/summary")
async def get_account_summary() -> Dict:
    """Get account summary if connected"""
    if not ib.isConnected():
        await connect_to_ibkr()
    
    try:
        # Request account summary
        account = await ib.accountSummaryAsync()
        await asyncio.sleep(1)
        return {"account_summary": util.df(account).to_dict()}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error fetching account summary: {str(e)}"
        )

class OptionChainRequest(BaseModel):
    symbol: str = Field(..., description="The underlying stock symbol (e.g., 'AAPL')")
    exchange: str = Field(default="NASDAQBX", description="Exchange (default: NASDAQBX)")
    currency: str = Field(default="USD", description="Currency (default: USD)")
    right: Optional[OptionType] = Field(None, description="Option type (CALL or PUT, optional)")

class OptionChainDetailRequest(BaseModel):
    symbol: str = Field(..., description="The underlying stock symbol (e.g., 'AAPL')")
    expiration_date: str = Field(..., description="Option expiration date (YYYYMMDD format)")
    right: Optional[OptionType] = Field(None, description="Option type (CALL or PUT, optional)")
    exchange: str = Field(default="NASDAQBX", description="Exchange (default: NASDAQBX)")
    currency: str = Field(default="USD", description="Currency (default: USD)")

    @validator('expiration_date')
    def validate_expiration_date(cls, v):
        try:
            datetime.strptime(v, '%Y%m%d')
            return v
        except ValueError:
            raise ValueError('expiration_date must be in YYYYMMDD format')

class HistoricalDataRequest(BaseModel):
    symbol: str = Field(..., description="The stock symbol (e.g., 'AAPL')")
    exchange: str = Field(default="SMART", description="Exchange (default: SMART)")
    currency: str = Field(default="USD", description="Currency (default: USD)")
    duration: str = Field(
        default="600 S",
        description="Time period (e.g., '1 D', '1 W', '1 M', '1 Y')"
    )
    bar_size: str = Field(
        default="1 min",
        description="Bar size (e.g., '1 min', '5 mins', '1 hour', '1 day')"
    )
    what_to_show: str = Field(
        default="TRADES",
        description="Type of data (TRADES, MIDPOINT, BID, ASK, etc.)"
    )

@app.post("/historical-data")
async def get_historical_data(request: HistoricalDataRequest) -> Dict:
    """
    Get historical market data for a specific symbol
    """
    if not ib.isConnected():
        await connect_to_ibkr()
    
    try:
        # Create contract object
        contract = Stock(request.symbol, request.exchange, request.currency)
        
        # Request historical data
        bars = await ib.reqHistoricalDataAsync(
            contract,
            endDateTime='',  # Empty string means now
            durationStr=request.duration,
            barSizeSetting=request.bar_size,
            whatToShow=request.what_to_show,
            useRTH=True,  # Regular Trading Hours only
            formatDate=1   # Format dates as 'YYYYMMDD HH:mm:ss'
        )
        
        # Convert the data to a more friendly format
        if bars:
            data = [{
                'date': bar.date.strftime('%Y-%m-%d %H:%M:%S'),
                'open': bar.open,
                'high': bar.high,
                'low': bar.low,
                'close': bar.close,
                'volume': bar.volume,
                'average': bar.average,
                'barCount': bar.barCount
            } for bar in bars]
            
            return {
                "symbol": request.symbol,
                "exchange": request.exchange,
                "currency": request.currency,
                "data": data
            }
        else:
            return {
                "symbol": request.symbol,
                "exchange": request.exchange,
                "currency": request.currency,
                "data": []
            }
            
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error fetching historical data: {str(e)}"
        )